import numpy as np

from algowinbet.calibration import IsotonicCalibrator, PlattCalibrator, ece, fit_ensemble_weight, log_loss
from algowinbet.domain import utc
from algowinbet.providers import FootballDataCSV


def test_platt_repairs_overconfidence():
    rng = np.random.default_rng(0)
    true_p = rng.uniform(0.1, 0.9, 4000)
    y = (rng.random(4000) < true_p).astype(float)
    over = 1 / (1 + np.exp(-2.0 * np.log(true_p / (1 - true_p))))  # too extreme
    cal = PlattCalibrator.fit(over, y)
    assert log_loss(cal.transform(over), y) < log_loss(over, y)
    assert ece(cal.transform(over), y) < ece(over, y)


def test_isotonic_is_monotone():
    rng = np.random.default_rng(1)
    p = rng.random(500)
    y = (rng.random(500) < p).astype(float)
    out = IsotonicCalibrator.fit(p, y).transform(np.linspace(0, 1, 50))
    assert (np.diff(out) >= -1e-12).all()


def test_ensemble_weight_prefers_informative_model():
    rng = np.random.default_rng(2)
    truth = rng.uniform(0.2, 0.8, 3000)
    y = (rng.random(3000) < truth).astype(float)
    noisy_market = np.clip(truth + rng.normal(0, 0.15, 3000), 0.02, 0.98)
    w, _ = fit_ensemble_weight(truth, noisy_market, y)
    assert w > 0.7


def test_football_data_csv_adapter(tmp_path):
    f = tmp_path / "E0.csv"
    f.write_text(
        "Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,B365H,B365D,B365A,B365CH,B365CD,B365CA,B365>2.5,B365<2.5\n"
        "E0,17/08/2024,15:00,Arsenal,Wolves,2,0,1.3,5.5,10,1.28,5.6,11,1.5,2.6\n"
        "E0,17/08/2099,15:00,Chelsea,Everton,,,1.6,4.0,5.5,,,,1.7,2.1\n"
    )
    p = FootballDataCSV([f])
    hist = p.list_history(None, utc(2025, 1, 1))
    assert len(hist) == 1
    fx = p.list_fixtures(None, utc(2099, 1, 1), utc(2100, 1, 1))
    assert len(fx) == 1
    assert {x.market_code for x in p.get_quotes(fx[0].id)} == {"MATCH_1X2", "TOTAL_GOALS"}
    assert {x.kind for x in p.get_quotes(hist[0].fixture_id)} == {"open", "close"}
