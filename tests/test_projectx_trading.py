from __future__ import annotations

import unittest

import _bootstrap  # noqa: F401
from projectx import ProjectXClient


class FakeProjectXClient(ProjectXClient):
    def __init__(self) -> None:
        super().__init__(base_url="https://example.invalid", token="token")
        self.calls: list[tuple[str, dict]] = []

    def _post(self, path: str, payload: dict, include_auth: bool = True) -> dict:
        self.calls.append((path, payload))
        if path == "/api/Account/search":
            return {
                "success": True,
                "accounts": [
                    {
                        "id": 123,
                        "name": "Practice",
                        "canTrade": True,
                        "isVisible": True,
                        "simulated": True,
                    }
                ],
            }
        if path == "/api/Order/place":
            return {"success": True, "orderId": 456, "errorCode": 0, "errorMessage": None}
        if path == "/api/Position/searchOpen":
            return {
                "success": True,
                "positions": [
                    {
                        "id": 1,
                        "accountId": 123,
                        "contractId": "CON.F.US.MNQ.M26",
                        "creationTimestamp": "2026-06-11T14:30:00Z",
                        "type": 1,
                        "size": 1,
                        "averagePrice": 30000.25,
                    }
                ],
            }
        if path == "/api/Position/closeContract":
            return {"success": True}
        return {"success": True}


class ProjectXTradingTests(unittest.TestCase):
    def test_search_accounts_maps_payload(self) -> None:
        client = FakeProjectXClient()

        accounts = client.search_accounts()

        self.assertEqual(accounts[0].id, 123)
        self.assertEqual(accounts[0].name, "Practice")
        self.assertTrue(accounts[0].can_trade)
        self.assertEqual(client.calls[0], ("/api/Account/search", {"onlyActiveAccounts": True}))

    def test_place_market_order_payload_includes_stop_bracket(self) -> None:
        client = FakeProjectXClient()

        result = client.place_market_order(
            account_id=123,
            contract_id="CON.F.US.MNQ.M26",
            side=0,
            size=1,
            custom_tag="axiom-test",
            stop_loss_ticks=8,
        )

        self.assertEqual(result.order_id, 456)
        _, payload = client.calls[-1]
        self.assertEqual(payload["accountId"], 123)
        self.assertEqual(payload["type"], 2)  # market
        self.assertEqual(payload["side"], 0)  # buy/bid
        self.assertEqual(payload["stopLossBracket"], {"ticks": 8, "type": 4})

    def test_position_search_and_close_contract(self) -> None:
        client = FakeProjectXClient()

        positions = client.search_open_positions(123)
        client.close_contract_position(account_id=123, contract_id="CON.F.US.MNQ.M26")

        self.assertEqual(positions[0].direction, 1)
        self.assertEqual(positions[0].size, 1)
        self.assertEqual(
            client.calls[-1],
            (
                "/api/Position/closeContract",
                {"accountId": 123, "contractId": "CON.F.US.MNQ.M26"},
            ),
        )


if __name__ == "__main__":
    unittest.main()
