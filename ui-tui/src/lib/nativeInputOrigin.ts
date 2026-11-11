const receipts = new WeakMap<object, string>()

/** Called only by the interactive composer, before any interpolation or transport. */
export function nativeTypedInput(text: string): object {
  const receipt = {}
  receipts.set(receipt, text)

  return receipt
}

export function verifiedTypedInput(receipt: object | undefined, text: string): boolean {
  return receipt !== undefined && receipts.get(receipt) === text
}
