import asyncio
import decimal
import json
import time
import pandas
from src.compute import (
    compute_borrowings_usd,
    compute_health_factor,
    compute_risk_adjusted_collateral_usd,
    decimal_range,
    get_amm_supply_at_price,
    simulate_liquidations_under_absolute_price_change,
)
import src.db as db
import src.constants as constants
from src.classes import Prices, State
from src.data import get_events
from src.swap_liquidity import get_jediswap


JEDI_SWAP = asyncio.run(get_jediswap())

def get_range(start, stop, step):
    return [
        x
        for x in decimal_range(
            # TODO: make it dependent on the collateral token .. use prices.prices[COLLATERAL_TOKEN]
            start=decimal.Decimal(start),
            stop=decimal.Decimal(stop),
            # TODO: make it dependent on the collateral token
            step=decimal.Decimal(step),
        )
    ]


# TODO: move this somewhere
def get_pair_range(c, b):
    if c == "ETH" and b == "wBTC":
        return get_range("0", "0.2", "0.0015")
    if c == "wBTC" and b == "ETH":
        return get_range("0", "25", "0.375")
    if c == "ETH":
        return get_range("50", "2500", "50")
    if c == "wBTC":
        return get_range("250", "32000", "250")
    raise ValueError(f"Wrong pair {c}-{b}")


def load_graph_data(state, prices, collateral_token, borrowings_token):
    data = pandas.DataFrame(
        {"collateral_token_price": get_pair_range(
            collateral_token, borrowings_token)},
    )
    # TOOD: needed?
    # data['collateral_token_price_multiplier'] = data['collateral_token_price_multiplier'].map(decimal.Decimal)
    data["max_borrowings_to_be_liquidated"] = data["collateral_token_price"].apply(
        lambda x: simulate_liquidations_under_absolute_price_change(
            prices=prices,
            collateral_token=collateral_token,
            collateral_token_price=x,
            state=state,
            borrowings_token=borrowings_token,
        )
    )

    # TODO
    data["max_borrowings_to_be_liquidated_at_interval"] = (
        data["max_borrowings_to_be_liquidated"].diff().abs()
    )
    # TODO: drops also other NaN, if there are any
    data.dropna(inplace=True)

    data["amm_borrowings_token_supply"] = data["collateral_token_price"].apply(
        lambda x: get_amm_supply_at_price(
            collateral_token=collateral_token,
            collateral_token_price=x,
            borrowings_token=borrowings_token,
            amm=JEDI_SWAP,
        )
    )
    return data


def get_events() -> pandas.DataFrame:
    connection = db.establish_connection()
    zklend_events = pandas.read_sql(
        sql=f"""
      SELECT
          *
      FROM
          starkscan_events
      WHERE
          from_address='{constants.Protocol.ZKLEND.value}'
      AND
          key_name IN ('Deposit', 'Withdrawal', 'CollateralEnabled', 'CollateralDisabled', 'Borrowing', 'Repayment', 'Liquidation', 'AccumulatorsSync')
      ORDER BY
          block_number, id ASC;
      """,
        con=connection,
    )
    connection.close()
    zklend_events.set_index("id", inplace=True)
    return zklend_events


def update_data():
    print("Updating CSV data...")
    state = State()

    zklend_events = get_events()

    for _, event in zklend_events.iterrows():
        state.process_event(event)

    prices = Prices()

    pairs = [
        # ("wBTC", "ETH"),
        # ("ETH", "wBTC"),
        ("ETH", "USDC"),
        ("ETH", "USDT"),
        ("ETH", "DAI"),
        ("wBTC", "USDC"),
        ("wBTC", "USDT"),
        ("wBTC", "DAI"),
    ]

    for pair in pairs:
        c = pair[0]
        b = pair[1]
        data = load_graph_data(
            state=state, prices=prices, collateral_token=c, borrowings_token=b
        )
        filename = f"data/{c}-{b}.csv"
        data.to_csv(filename, index=False)
        print(pair, "done")

    histogram_data = [
        {
            "token": token,
            "borrowings": user_state.token_states[token].borrowings
            * prices.prices[token]
            / 10 ** constants.get_decimals(token),
        }
        for user_state in state.user_states.values()
        for token in constants.symbol_decimals_map.keys()
        if token[0] != "z"
    ]

    pandas.DataFrame(histogram_data).to_csv("data/histogram.csv", index=False)

    for user, user_state in state.user_states.items():
        risk_adjusted_collateral_usd = compute_risk_adjusted_collateral_usd(
            user_state=user_state,
            prices=prices.prices,
        )
        borrowings_usd = compute_borrowings_usd(
            user_state=user_state,
            prices=prices.prices,
        )
        health_factor = compute_health_factor(
            risk_adjusted_collateral_usd=risk_adjusted_collateral_usd,
            borrowings_usd=borrowings_usd,
        )
        user_state.health_factor = health_factor

    class BadUser:
        def __init__(self, address, user_state):
            self.health_factor = user_state.health_factor
            self.address = address
            self.loan_size = decimal.Decimal("0")
            self.collateral_size = decimal.Decimal("0")
            self.collateral = {}
            self.borrowings = {}
            self.formatted_collateral = ""
            self.formatted_borrowings = ""
            self.calculate(user_state)
            self.format()

        def calculate(self, user_state):
            for token, token_state in user_state.token_states.items():
                if token_state.z_token:
                    continue
                if token_state.borrowings > decimal.Decimal("0"):
                    tokens = (
                        token_state.borrowings /
                        constants.TOKEN_DECIMAL_FACTORS[token]
                    )
                    self.borrowings[token] = (
                        self.borrowings.get(
                            token, decimal.Decimal("0")) + tokens
                    )
                    self.loan_size += tokens * prices.prices[token]
                if (
                    token_state.collateral_enabled
                    and token_state.deposit > decimal.Decimal("0")
                ):
                    tokens = (
                        token_state.deposit /
                        constants.TOKEN_DECIMAL_FACTORS[token]
                    )
                    self.collateral[token] = (
                        self.collateral.get(
                            token, decimal.Decimal("0")) + tokens
                    )
                    self.collateral_size += (
                        tokens
                        * prices.prices[token]
                        * constants.COLLATERAL_FACTORS[token]
                    )

        def format(self):
            for token, size in self.collateral.items():
                if self.formatted_collateral != "":
                    self.formatted_collateral += ", "
                formatted_size = format(size, ".4f")
                self.formatted_collateral += f"{token}: {formatted_size}"
            for token, size in self.borrowings.items():
                if self.formatted_borrowings != "":
                    self.formatted_borrowings += ", "
                formatted_size = format(size, ".4f")
                self.formatted_borrowings += f"{token}: {formatted_size}"

    small_bad_users = []
    big_bad_users = []

    for user, user_state in state.user_states.items():
        if user_state.health_factor < 1 and user_state.health_factor > 0.7:
            bad_user = BadUser(address=user, user_state=user_state)
            if bad_user.loan_size > 100:
                big_bad_users.append(bad_user)
            else:
                small_bad_users.append(bad_user)

    big_bad_users = sorted(
        big_bad_users, key=lambda x: x.health_factor, reverse=False)
    small_bad_users = sorted(
        small_bad_users, key=lambda x: x.health_factor, reverse=False
    )

    n = 20

    bbu_data = {
        "User": [user.address for user in big_bad_users[:n]],
        "Health factor": [user.health_factor for user in big_bad_users[:n]],
        "Borrowing in USD": [user.loan_size for user in big_bad_users[:n]],
        "Risk adjusted collateral in USD": [
            user.collateral_size for user in big_bad_users[:n]
        ],
        "Collateral": [user.formatted_collateral for user in big_bad_users[:n]],
        "Borrowings": [user.formatted_borrowings for user in big_bad_users[:n]],
    }

    sbu_data = {
        "User": [user.address for user in small_bad_users[:n]],
        "Health factor": [user.health_factor for user in small_bad_users[:n]],
        "Borrowing in USD": [user.loan_size for user in small_bad_users[:n]],
        "Risk adjusted collateral in USD": [
            user.collateral_size for user in small_bad_users[:n]
        ],
        "Collateral": [user.formatted_collateral for user in small_bad_users[:n]],
        "Borrowings": [user.formatted_borrowings for user in small_bad_users[:n]],
    }

    pandas.DataFrame(bbu_data).to_csv(
        "data/large_loans_sample.csv", index=False)
    pandas.DataFrame(sbu_data).to_csv(
        "data/small_loans_sample.csv", index=False)

    max_block_number = zklend_events["block_number"].max()
    max_timestamp = zklend_events["timestamp"].max()

    dict = {"timestamp": str(max_timestamp),
            "block_number": str(max_block_number)}

    with open("data/last_update.json", "w") as outfile:
        outfile.write(json.dumps(dict))

    print("Updated CSV data")


def update_data_recursively():
    update_data()
    time.sleep(120)
    update_data_recursively()


if __name__ == "__main__":
    update_data()
