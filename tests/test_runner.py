from bot.runner import Runner


def sampler(samples, until=17):
    r = Runner.__new__(Runner)   # skip __init__: no game, input or screen needed
    r.sample_until, r.samples = until, samples
    return r


def test_sample_summary_counts_passes_and_losses_per_round():
    r = sampler([17, None, 15, None, 15])
    assert r.sample_summary() == "5 sample(s): 2 passed round 17; lost on round 15: 2, round 17: 1"


def test_sample_summary_all_passed_and_empty():
    assert sampler([None, None]).sample_summary() == "2 sample(s): 2 passed round 17"
    assert sampler([]).sample_summary() == "0 sample(s): 0 passed round 17"
