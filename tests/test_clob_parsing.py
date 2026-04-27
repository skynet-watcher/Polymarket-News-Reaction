from app.jobs.sync_markets import parse_clob_best_prices


def test_parse_clob_best_prices_list_levels():
    data = {"bids": [[0.41, 100], [0.40, 50]], "asks": [[0.43, 100], [0.44, 10]]}
    bid, ask = parse_clob_best_prices(data)
    assert bid == 0.41
    assert ask == 0.43


def test_parse_clob_best_prices_dict_levels():
    data = {
        "bids": [{"price": "0.51", "size": "10"}, {"price": "0.50", "size": "99"}],
        "asks": [{"price": "0.53", "size": "10"}, {"price": "0.54", "size": "99"}],
    }
    bid, ask = parse_clob_best_prices(data)
    assert bid == 0.51
    assert ask == 0.53


def test_parse_clob_best_prices_missing_or_invalid():
    bid, ask = parse_clob_best_prices({"bids": [], "asks": []})
    assert bid is None
    assert ask is None

