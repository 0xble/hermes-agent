# Behavioral uv validation, adapted from the pre-PM suite (d0dbf2cbb697).
# PM replaced PATH-shim salvage with a pinned artifact. Keep testing the real
# spawn/exit-code and relocated-launcher boundary under both PS 5.1 and pwsh 7.
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$installScript = Join-Path $repoRoot 'scripts\install.ps1'
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('hermes-uv-shim-test-' + [Guid]::NewGuid().ToString('N'))
$originalPath = $env:PATH
$script:Failures = 0
function Assert-Equal($Expected, $Actual, [string]$Label) {
    if ($Expected -ceq $Actual) { Write-Host "PASS: $Label" }
    else {
        Write-Host "FAIL: $Label (expected [$Expected], actual [$Actual])"
        $script:Failures++
    }
}
function New-FakeExe([string]$Name, [string]$Source) {
    $srcPath = Join-Path $testRoot "$Name.cs"
    $exePath = Join-Path $testRoot "$Name.exe"
    [IO.File]::WriteAllText($srcPath, $Source)
    # PS 5.1's compiler emits a real Windows console exe, even when the
    # enclosing suite runs in pwsh (whose Add-Type cannot emit an exe).
    $winPs = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $srcQuoted = $srcPath.Replace("'", "''")
    $exeQuoted = $exePath.Replace("'", "''")
    & $winPs -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Add-Type -Path '$srcQuoted' -OutputAssembly '$exeQuoted' -OutputType ConsoleApplication" | Out-Null
    if ($LASTEXITCODE -or -not (Test-Path $exePath)) { throw "failed to compile $Name" }
    return $exePath
}
function Set-TestArchive([string]$Binary) {
    $payload = Join-Path $testRoot 'payload'
    New-Item -ItemType Directory -Force -Path $payload | Out-Null
    Copy-Item -LiteralPath $Binary -Destination (Join-Path $payload 'uv.exe') -Force
    $script:FixtureArchive = Join-Path $testRoot 'fixture.zip'
    Compress-Archive -Path (Join-Path $payload 'uv.exe') -DestinationPath $script:FixtureArchive -Force
    $script:UvPinFiles[$target] = @{
        Url = 'https://fixture.invalid/uv.zip'; MirrorUrl = ''
        Sha256 = (Get-FileHash -LiteralPath $script:FixtureArchive -Algorithm SHA256).Hash
    }
}
try {
    # Tripwires precede dot-sourcing: definitions must not run an install.
    function Invoke-WebRequest { throw 'unexpected network access' }
    function Invoke-RestMethod { throw 'unexpected network access' }
    function git { throw 'unexpected git command' }
    . $installScript -HermesHome (Join-Path $testRoot 'home') -InstallDir (Join-Path $testRoot 'checkout')
    Assert-Equal $false (Test-Path $testRoot) 'dot-source loads definitions without writes'
    New-Item -ItemType Directory -Force -Path $testRoot | Out-Null
    $okExe = New-FakeExe 'fake-uv-ok' @'
public static class FakeUv {
    public static int Main(string[] args) {
        System.Console.WriteLine("uv 99.0.0");
        return 0;
    }
}
'@
    $shimExe = New-FakeExe 'fake-uv-shim' @'
using System.IO;
public static class FakeUvShim {
    public static int Main(string[] args) {
        string self = System.Reflection.Assembly.GetExecutingAssembly().Location;
        string target = Path.GetFullPath(Path.Combine(Path.GetDirectoryName(self), "..", "lib", "uv", "tools", "uv.exe"));
        if (File.Exists(target)) {
            System.Console.WriteLine("uv 99.0.0");
            return 0;
        }
        System.Console.Error.WriteLine("relocated uv launcher: target missing");
        return 1;
    }
}
'@
    $chocoRoot = Join-Path $testRoot 'chocolatey'
    $chocoBin = Join-Path $chocoRoot 'bin'
    $chocoTools = Join-Path $chocoRoot 'lib\uv\tools'
    New-Item -ItemType Directory -Force -Path $chocoBin, $chocoTools | Out-Null
    $chocoShim = Join-Path $chocoBin 'uv.exe'
    Copy-Item $shimExe $chocoShim
    Copy-Item $okExe (Join-Path $chocoTools 'uv.exe')
    Assert-Equal $true (Test-UvAtLeastPin $okExe) 'standalone uv meets the pin'
    Assert-Equal $true (Test-UvAtLeastPin $chocoShim) 'launcher works at its original location'
    Assert-Equal $false (Test-UvAtLeastPin $shimExe) 'relocated launcher is rejected by its real exit code'
    $badExit = Join-Path $testRoot 'nonzero.cmd'
    "@echo uv 99.0.0`r`n@exit /b 1" | Set-Content -LiteralPath $badExit -Encoding Ascii
    Assert-Equal $false (Test-UvAtLeastPin $badExit) 'nonzero exit rejects even a plausible version line'
    $oldUv = Join-Path $testRoot 'old.cmd'
    "@echo uv 0.1.0`r`n@exit /b 0" | Set-Content -LiteralPath $oldUv -Encoding Ascii
    Assert-Equal $false (Test-UvAtLeastPin $oldUv) 'an older uv is rejected'

    # Stub only the network transport. Hash verification, extraction, staging,
    # cached-binary validation and final validation use the real installer.
    $script:DownloadCalls = 0
    function Invoke-DownloadWithProgress([string]$Uri, [string]$OutFile) {
        Assert-Equal 'https://fixture.invalid/uv.zip' $Uri 'download uses the pinned URL'
        $script:DownloadCalls++
        Copy-Item -LiteralPath $script:FixtureArchive -Destination $OutFile -Force
    }
    function Get-PmStoreRoot { return (Join-Path $testRoot 'store') }
    $target = "win32-$(Get-WindowsArch)"
    $managedUv = Join-Path (Get-PmStoreRoot) "uv-$($script:UvPinVersion)-$target\uv.exe"
    $env:PATH = "$chocoBin;$originalPath"
    Set-TestArchive $okExe
    Assert-Equal $managedUv (Get-Uv) 'PATH launcher is ignored in favor of the pinned store'
    Assert-Equal 1 $script:DownloadCalls 'a fresh store downloads its own artifact'
    Assert-Equal (Get-FileHash $okExe).Hash (Get-FileHash $managedUv).Hash 'the standalone artifact is staged, not the PATH shim'
    Assert-Equal $managedUv (Get-Uv) 'a working managed uv is reused'
    Assert-Equal 1 $script:DownloadCalls 'working cached uv needs no download'

    Copy-Item $shimExe $managedUv -Force
    Assert-Equal $managedUv (Get-Uv) 'a broken cached launcher is replaced'
    Assert-Equal 2 $script:DownloadCalls 'broken cached uv triggers a download'
    Assert-Equal $true (Test-UvAtLeastPin $managedUv) 'replacement runs at its managed location'

    Remove-Item -LiteralPath $managedUv -Force
    Set-TestArchive $shimExe
    $failed = $false
    try { Get-Uv | Out-Null } catch { $failed = $_.Exception.Message -like '*staged but does not run*' }
    Assert-Equal $true $failed 'final validation rejects an archive containing a relocated launcher'
    Set-TestArchive $okExe
    Assert-Equal $managedUv (Get-Uv) 'rerun recovers from a previously staged broken launcher'

    Remove-Item -LiteralPath $managedUv -Force
    $script:UvPinFiles[$target].Sha256 = '0' * 64
    $failed = $false
    try { Get-Uv | Out-Null } catch { $failed = $_.Exception.Message -like '*download digest mismatch*' }
    Assert-Equal $true $failed 'hash mismatch fails instead of falling back to a PATH shim'
    Assert-Equal $false (Test-Path $managedUv) 'unverified bytes are not staged'
} finally {
    $env:PATH = $originalPath
    if (Test-Path $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
}
if ($script:Failures) { exit 1 }
Write-Host 'all assertions passed'
