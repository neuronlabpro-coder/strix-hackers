"""Un acuerdo Enterprise solo cambia las features de su organización."""

from backend.apps.commercial.service import resolve_feature_value


def test_override_explicito_precede_al_plan_y_al_default() -> None:
    assert resolve_feature_value(override=False, plan_enabled=True) is False
    assert resolve_feature_value(override=True, plan_enabled=False) is True
    assert resolve_feature_value(override=None, plan_enabled=True) is True
    assert resolve_feature_value(override=None, plan_enabled=None) is False
