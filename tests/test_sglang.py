import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import promparse
import dashboard

# Minimal realistic SGLang /metrics surface: token counters carry an
# is_streaming={false,true} split, gauges are 0-1 fractions, latency
# histograms are multi-labeled.
SG = """# TYPE sglang:prompt_tokens_total counter
sglang:prompt_tokens_total{is_streaming="false",model_name="main"} 815.0
sglang:prompt_tokens_total{is_streaming="true",model_name="main"} 15.0
# TYPE sglang:generation_tokens_total counter
sglang:generation_tokens_total{is_streaming="false",model_name="main"} 1011.0
sglang:generation_tokens_total{is_streaming="true",model_name="main"} 9.0
# TYPE sglang:num_requests_total counter
sglang:num_requests_total{model_name="main"} 4.0
# TYPE sglang:num_running_reqs gauge
sglang:num_running_reqs{model_name="main"} 2.0
# TYPE sglang:num_queue_reqs gauge
sglang:num_queue_reqs{model_name="main"} 1.0
# TYPE sglang:full_token_usage gauge
sglang:full_token_usage{model_name="main"} 0.42
# TYPE sglang:cache_hit_rate gauge
sglang:cache_hit_rate{model_name="main"} 0.73
# TYPE sglang:spec_accept_rate gauge
sglang:spec_accept_rate{model_name="main"} 0.4214285714
# TYPE sglang:spec_accept_length gauge
sglang:spec_accept_length{model_name="main"} 3.95
# TYPE sglang:num_retracted_reqs gauge
sglang:num_retracted_reqs{model_name="main"} 3.0
# TYPE sglang:time_to_first_token_seconds histogram
sglang:time_to_first_token_seconds_bucket{is_streaming="false",le="0.5"} 10.0
sglang:time_to_first_token_seconds_bucket{is_streaming="false",le="1.0"} 25.0
sglang:time_to_first_token_seconds_bucket{is_streaming="false",le="+Inf"} 30.0
sglang:time_to_first_token_seconds_sum{is_streaming="false"} 5.0
sglang:time_to_first_token_seconds_count{is_streaming="false"} 30.0
sglang:time_to_first_token_seconds_bucket{is_streaming="true",le="0.5"} 4.0
sglang:time_to_first_token_seconds_bucket{is_streaming="true",le="1.0"} 10.0
sglang:time_to_first_token_seconds_bucket{is_streaming="true",le="+Inf"} 12.0
sglang:time_to_first_token_seconds_sum{is_streaming="true"} 2.0
sglang:time_to_first_token_seconds_count{is_streaming="true"} 12.0
"""


def main():
    p = dashboard._with_sglang_aliases(promparse.parse(SG))
    ctr, g = p["counters"], p["gauges"]
    # detection
    assert dashboard._is_sglang_sample(p), "should be detected as sglang"
    # multi-label counters summed into the vLLM-shaped aliases
    assert ctr["vllm:prompt_tokens_total"]["value"] == 830.0, ctr.get("vllm:prompt_tokens_total")
    assert ctr["vllm:generation_tokens_total"]["value"] == 1020.0
    assert ctr["vllm:request_success_total"]["value"] == 4.0
    # gauges copied
    assert g["vllm:num_requests_running"]["value"] == 2.0
    assert g["vllm:num_requests_waiting"]["value"] == 1.0
    # fractions rescaled to 0-100
    assert abs(g["vllm:kv_cache_usage_perc"]["value"] - 42.0) < 1e-6, g.get("vllm:kv_cache_usage_perc")
    assert abs(g["vllm:prefix_cache_hit_rate"]["value"] - 73.0) < 1e-6
    # original sglang gauges preserved (spec read directly)
    assert g["sglang:spec_accept_rate"]["value"] == 0.4214285714
    assert g["sglang:spec_accept_length"]["value"] == 3.95
    # NOT aliased into a counter (it's a gauge)
    assert "vllm:num_reemptions_total" not in ctr
    # latency histogram copied under the vllm: name, buckets merged across
    # is_streaming label sets (30+12=42 obs), single ladder
    vh = p["histograms"]["vllm:time_to_first_token_seconds"]
    assert vh["count"] == 42.0, vh["count"]
    got = dict(vh["buckets"])
    assert got[0.5] == 14.0 and got[1.0] == 35.0 and got[float("inf")] == 42.0, got
    assert len(vh["buckets"]) == 3
    # the original sglang histogram is also present and independently merged
    sh = p["histograms"]["sglang:time_to_first_token_seconds"]
    assert sh["count"] == 42.0 and len(sh["buckets"]) == 3
    # percentile of the merged histogram is sensible (p50 in [0.5,1.0])
    p50 = promparse.percentile(vh["buckets"], 50)
    assert p50 is not None and 0.5 <= p50 <= 1.0, p50
    print("OK test_sglang")


if __name__ == "__main__":
    main()
