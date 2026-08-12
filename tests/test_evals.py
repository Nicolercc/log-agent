from evals.run import load_fixtures, run_eval


def test_eval_harness_runs_against_redacted_example():
    fixtures = load_fixtures(__import__("pathlib").Path("evals/fixtures.example.jsonl"))
    report = run_eval(fixtures, use_expected=True)

    assert report["fixtures"] == 8
    assert report["misclassifications_reached_events"] == 0
    assert 0.0 <= report["precision"] <= 1.0
    assert 0.0 <= report["recall"] <= 1.0
