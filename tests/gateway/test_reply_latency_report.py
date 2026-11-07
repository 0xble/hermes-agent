"""Date cohorts count inbound messages once, including incomplete observations."""

import json


def test_report_groups_admission_ids_and_reports_coverage_and_linear_quantiles(tmp_path):
    from gateway.reply_latency_report import read_records, summarize

    identity = dict(platform="telegram", profile="default", chat_id="c", thread_id="t")
    rows = []
    for index, seconds in enumerate([1, 3, 5, 7, 9]):
        base = dict(identity, message_id=str(index), turn_id="transport")
        rows += [dict(base, event="received", timestamp=100),
                 dict(base, event="received", timestamp=100, turn_id="admission"),
                 dict(base, event="agent_start", timestamp=101, turn_id="admission"),
                 dict(base, event="first_reply", timestamp=100 + seconds, delivery_kind="progress_send"),
                 dict(base, event="final_delivery", timestamp=110 + seconds, delivery_kind="text_send")]
    rows += [dict(identity, message_id="missing", event="received", timestamp=100),
             dict(identity, message_id="old", event="received", timestamp=10),
             dict(identity, message_id="old", event="first_reply", timestamp=105),
             dict(identity, message_id="missing", event="first_reply", timestamp=106)]
    (tmp_path / "2026-11-07.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n{partial")
    result = summarize(read_records(tmp_path), since=99, until=110, platform="telegram")
    assert result["inbounds"] == 6
    assert result["first_visible"] == {"n": 5, "p50_s": 5.0, "p90_s": 8.2}
    assert result["final_delivery"] == {"n": 5, "p50_s": 15.0, "p90_s": 18.2}
    assert result["agent_start"]["n"] == 5
    assert result["missing_first_visible"] == 1
