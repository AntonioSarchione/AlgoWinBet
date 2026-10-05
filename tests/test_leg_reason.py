"""Where a selection's value comes from: the model's view or only Sisal's price (user's request, 2026-10-05)."""
from algowinbet.domain import Opportunity, OpportunityStatus
from algowinbet.explain import leg_reason


def _o(p, pm, status="CANDIDATE", odds=2.1):
    return Opportunity.model_construct(p_final=p, p_market=pm, status=OpportunityStatus(status), odds=odds,
                                       data_quality_parts={"lineup": 0.5})


def test_kinds():
    assert leg_reason(_o(0.51, 0.505))[0] == "prezzo"  # model = Pinnacle: the value is Sisal's price only
    assert leg_reason(_o(0.55, 0.50))[0] == "modello" and "Formazioni non ancora note" in leg_reason(_o(0.55, 0.50))[1]
    assert leg_reason(_o(0.48, 0.50))[0] == "contro"
    assert leg_reason(_o(0.70, 0.70, "FAIR", 1.40))[0] == "equa"
    assert leg_reason(_o(0.60, None))[0] == "solo-modello"
