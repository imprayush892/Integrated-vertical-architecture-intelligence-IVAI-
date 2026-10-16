"""Topology priors counted from municipal-approved Mumbai plans (data/rules/mumbai_topology_priors.json)."""
from planner_engine import diagram, rules


def test_priors_load_and_carry_evidence():
    tp = rules.topology_priors()
    assert tp, "packaged topology priors missing"
    for fig in tp["exterior"].values():
        f = fig.get("facade")
        if f:
            assert f["n_projects"] >= 5 and 0.0 <= f["p"] <= 1.0


def test_bedrooms_need_windows_but_priors_never_relax():
    assert diagram.prior_needs_window("bedroom", False)  # on the façade in ~89% of approved flats
    assert diagram.prior_needs_window("living", True)  # statutory default kept
    assert not diagram.prior_needs_window("passage", False)


def test_master_toilet_link_is_supported():
    assert diagram.prior_default_link("master", "toilet")
    assert not diagram.prior_default_link("living", "toilet")
