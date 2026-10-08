"""Readiness script helpers and offline dry run; no AWS calls."""

import check_readiness


def test_principal_arn_conversion_and_masking():
    assert check_readiness.principal_arn(
        "arn:aws:sts::123456789012:assumed-role/ReviewRole/session"
    ) == "arn:aws:iam::123456789012:role/ReviewRole"
    assert check_readiness.principal_arn("arn:aws:iam::123456789012:user/alice") == "arn:aws:iam::123456789012:user/alice"
    assert check_readiness.principal_arn("arn:aws:iam::123456789012:root") is None
    assert check_readiness.mask_account("arn:aws:iam::123456789012:user/alice") == "arn:aws:iam::1234****9012:user/alice"


def test_dry_run_only_checks_boto3_api_surface(capsys):
    assert check_readiness.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "OK    boto3 ce.get_cost_and_usage_comparisons" in out
    assert "OK    boto3 bedrock.get_foundation_model_availability" in out
    assert "OK    boto3 bedrock.get_use_case_for_model_access" in out
    assert "OK    boto3 freetier.get_account_plan_state" in out
    assert "FAIL" not in out


def test_ce_requires_months(capsys):
    try:
        check_readiness.main(["--ce"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("--ce without months must exit 2")
