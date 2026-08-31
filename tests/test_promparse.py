import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import promparse

SAMPLE = """# HELP vllm:prompt_tokens_total Prompt tokens
# TYPE vllm:prompt_tokens_total counter
vllm:prompt_tokens_total{engine="0",model_name="qwen"} 1234.0
# HELP vllm:kv_cache_usage_perc KV cache usage
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{engine="0",model_name="qwen"} 0.42
# TYPE vllm:time_to_first_token_seconds histogram
vllm:time_to_first_token_seconds_bucket{engine="0",le="0.01",model_name="qwen"} 10
vllm:time_to_first_token_seconds_bucket{engine="0",le="0.1",model_name="qwen"} 25
vllm:time_to_first_token_seconds_bucket{engine="0",le="+Inf",model_name="qwen"} 30
vllm:time_to_first_token_seconds_sum{engine="0",model_name="qwen"} 1.5
vllm:time_to_first_token_seconds_count{engine="0",model_name="qwen"} 30
"""

def main():
    m = promparse.parse(SAMPLE)
    assert m["counters"]["vllm:prompt_tokens_total"]["value"] == 1234.0
    assert m["gauges"]["vllm:kv_cache_usage_perc"]["value"] == 0.42
    h = m["histograms"]["vllm:time_to_first_token_seconds"]
    assert h["buckets"] == [(0.01, 10), (0.1, 25), (float("inf"), 30)], h["buckets"]
    assert h["sum"] == 1.5 and h["count"] == 30
    # 3 obs <= 0.01, 15 in (0.01,0.1], 5 in (0.1,+Inf]; P50 lands in (0.01,0.1]
    p50 = promparse.percentile(h["buckets"], 50)
    assert p50 is not None and 0.01 <= p50 <= 0.1, p50
    p95 = promparse.percentile(h["buckets"], 95)
    assert p95 is None, p95  # rank 27 falls in (0.1, +Inf] -> no finite bound
    # with a finite 1.0 band present, p99 interpolates in (0.1, 1.0]
    b2 = [(0.01, 10), (0.1, 25), (1.0, 30)]
    p99 = promparse.percentile(b2, 99)
    assert p99 is not None and 0.1 < p99 <= 1.0, p99
    # no data
    assert promparse.percentile([], 50) is None
    assert promparse.percentile([(0.1, 0.0)], 50) is None
    # multi-label gauge sums (vllm:num_requests_waiting_by_reason style)
    multi = """# TYPE vllm:num_requests_waiting_by_reason gauge
vllm:num_requests_waiting_by_reason{reason="a"} 2.0
vllm:num_requests_waiting_by_reason{reason="b"} 3.0
"""
    m2 = promparse.parse(multi)
    assert abs(m2["gauges"]["vllm:num_requests_waiting_by_reason"]["value"] - 5.0) < 1e-9
    # per-label access + sum_all (the sum(...) equivalent)
    assert promparse.value_by_label(m2, "gauges", "vllm:num_requests_waiting_by_reason",
                                    {"reason": "a"}) == 2.0
    assert promparse.value_by_label(m2, "gauges", "vllm:num_requests_waiting_by_reason",
                                    {"reason": "b"}) == 3.0
    assert promparse.value_by_label(m2, "gauges", "vllm:num_requests_waiting_by_reason",
                                    {"reason": "zz"}) is None
    assert promparse.sum_all(m2, "gauges", "vllm:num_requests_waiting_by_reason") == 5.0
    assert len(promparse.all_labels(m2, "gauges", "vllm:num_requests_waiting_by_reason")) == 2
    # counters keep per-label sets too; in-place update on repeat label set
    ct = """# TYPE vllm:requests counter
vllm:requests{reason="stop"} 3.0
vllm:requests{reason="length"} 5.0
"""
    m3 = promparse.parse(ct)
    assert promparse.sum_all(m3, "counters", "vllm:requests") == 8.0
    assert promparse.value_by_label(m3, "counters", "vllm:requests",
                                    {"reason": "stop"}) == 3.0
    m3b = promparse.parse(ct + 'vllm:requests{reason="stop"} 7.0\n')
    assert len(m3b["counters"]["vllm:requests"]["series"]) == 2
    assert promparse.value_by_label(m3b, "counters", "vllm:requests",
                                    {"reason": "stop"}) == 7.0
    # back-compat: first label-set value unchanged
    assert m3["counters"]["vllm:requests"]["value"] == 3.0
    # missing series
    assert promparse.sum_all(m3, "counters", "nope") is None
    assert promparse.all_labels(m3, "counters", "nope") == []
    # counter reset safety is the dashboard's job, parser just reports values
    # multi-label histogram (sglang's is_streaming={false,true} emits TWO full
    # bucket ladders + two _sum/_count per base name) must merge into ONE:
    # buckets summed per-le, sum and count summed across label sets.
    ml = """# TYPE sglang:e2e_request_latency_seconds histogram
sglang:e2e_request_latency_seconds_bucket{is_streaming="false",le="0.5"} 10.0
sglang:e2e_request_latency_seconds_bucket{is_streaming="false",le="1.0"} 25.0
sglang:e2e_request_latency_seconds_bucket{is_streaming="false",le="+Inf"} 30.0
sglang:e2e_request_latency_seconds_sum{is_streaming="false"} 5.0
sglang:e2e_request_latency_seconds_count{is_streaming="false"} 30.0
sglang:e2e_request_latency_seconds_bucket{is_streaming="true",le="0.5"} 4.0
sglang:e2e_request_latency_seconds_bucket{is_streaming="true",le="1.0"} 10.0
sglang:e2e_request_latency_seconds_bucket{is_streaming="true",le="+Inf"} 12.0
sglang:e2e_request_latency_seconds_sum{is_streaming="true"} 2.0
sglang:e2e_request_latency_seconds_count{is_streaming="true"} 12.0
"""
    mh = promparse.parse(ml)["histograms"]["sglang:e2e_request_latency_seconds"]
    # 42 observations total (30 non-streaming + 12 streaming); merged buckets
    # are cumulative and non-decreasing
    assert mh["count"] == 42.0, mh["count"]
    assert mh["sum"] == 7.0, mh["sum"]
    got = dict(mh["buckets"])
    assert got[0.5] == 14.0, mh["buckets"]   # 10 + 4
    assert got[1.0] == 35.0, mh["buckets"]   # 25 + 10
    assert got[float("inf")] == 42.0, mh["buckets"]  # 30 + 12
    assert len(mh["buckets"]) == 3, mh["buckets"]
    # single-label set still reduces to the exact single-ladder result
    one = promparse.parse(SAMPLE)["histograms"]["vllm:time_to_first_token_seconds"]
    assert dict(one["buckets"])[0.01] == 10.0 and one["count"] == 30.0
    print("OK test_promparse")

if __name__ == "__main__":
    main()
