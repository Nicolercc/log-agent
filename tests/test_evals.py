from evals.run import load_fixtures, run_eval


def test_eval_harness_runs_against_redacted_example():
    fixtures = load_fixtures(__import__("pathlib").Path("evals/fixtures.example.jsonl"))
    report = run_eval(fixtures, use_expected=True)

    assert report["fixtures"] == 8
    assert report["misclassifications_reached_events"] == 0
    assert 0.0 <= report["precision"] <= 1.0
    assert 0.0 <= report["recall"] <= 1.0


def test_eval_counts_wrong_kind_as_misclassification():
    class FakeClient:
        class Messages:
            def create(self, **kwargs):
                class Response:
                    pass

                class Content:
                    pass

                r = Response()
                c = Content()
                c.text = """[{
                    "gmail_msg_id": "fx",
                    "company": "Company A",
                    "role_hint": "Backend Engineer",
                    "kind": "screen",
                    "occurred_on": "2026-08-02",
                    "confidence": 0.99,
                    "evidence": "we received your application"
                }]"""
                r.content = [c]
                return r

        @property
        def messages(self):
            return self.Messages()

    fixtures = [{
        "id": "fx",
        "sender": "jobs@example.com",
        "subject": "Thanks",
        "body": "Hi, we received your application today.",
        "expect": {"kind": "confirmed", "company": "Company A", "should_commit": True},
    }]

    report = run_eval(fixtures, client=FakeClient())

    assert report["actual_commits"] == 1
    assert report["precision"] == 0.0
    assert report["misclassifications_reached_events"] == 1
