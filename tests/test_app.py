import importlib.util
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


APP_PATH = Path(__file__).parents[1] / "app" / "asin_product_watch.py"
SPEC = importlib.util.spec_from_file_location("asin_product_watch", APP_PATH)
app = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = app
SPEC.loader.exec_module(app)


class ProductWatchTests(unittest.TestCase):
    def test_extract_asins_from_text_and_urls(self):
        raw = "B09B2SBHQK, https://www.amazon.de/dp/B0ABC12345?tag=x b09b2sbhqk"
        self.assertEqual(app.extract_asins(raw), ["B09B2SBHQK", "B0ABC12345"])

    def test_export_contains_only_one_asin_per_line(self):
        exported = app.export_asin_list(
            {
                "asins": ["B09B2SBHQK", "B0ABC12345"],
                "csrf_token": "must-not-leak",
                "check_interval_minutes": 60,
            }
        )
        self.assertEqual(exported, b"B09B2SBHQK\nB0ABC12345\n")
        self.assertNotIn(b"must-not-leak", exported)

    def test_normalize_item_prefers_buy_box(self):
        item = {
            "asin": "B09B2SBHQK",
            "detailPageURL": "https://www.amazon.de/dp/B09B2SBHQK?tag=test",
            "parentASIN": "B000000000",
            "images": {"primary": {"medium": {"url": "https://img.example/product.jpg"}}},
            "itemInfo": {
                "title": {"displayValue": "Testprodukt", "locale": "de_DE"},
                "byLineInfo": {"brand": {"displayValue": "Testmarke"}},
                "features": {"displayValues": ["Merkmal A"]},
                "productInfo": {"color": {"displayValue": "Blau", "label": "Farbe"}},
            },
            "offersV2": {
                "listings": [
                    {"isBuyBoxWinner": False, "price": {"money": {"amount": 20, "currency": "EUR"}}},
                    {
                        "isBuyBoxWinner": True,
                        "availability": {"type": "IN_STOCK", "message": "Auf Lager"},
                        "price": {"money": {"amount": 19.99, "currency": "EUR", "displayAmount": "19,99 €"}},
                        "merchantInfo": {"name": "Amazon.de"},
                        "condition": {"value": "New"},
                    },
                ]
            },
        }
        snapshot = app.normalize_item(item)
        self.assertTrue(snapshot["accessible"])
        self.assertEqual(snapshot["title"], "Testprodukt")
        self.assertEqual(snapshot["brand"], "Testmarke")
        self.assertEqual(snapshot["price_amount"], 19.99)
        self.assertEqual(snapshot["availability_type"], "IN_STOCK")
        self.assertEqual(snapshot["product_info"], {"color": {"displayValue": "Blau"}})

    def test_missing_offer_is_unknown_not_out_of_stock(self):
        snapshot = app.normalize_item(
            {"asin": "B09B2SBHQK", "itemInfo": {"title": {"displayValue": "Testprodukt"}}}
        )
        self.assertEqual(snapshot["availability_type"], "NO_FEATURED_OFFER")
        self.assertEqual(app.format_availability(snapshot["availability_type"]), "kein Featured Offer in der API")

    def test_empty_out_of_stock_placeholder_is_no_featured_offer(self):
        snapshot = app.normalize_item(
            {
                "asin": "B09B2SBHQK",
                "itemInfo": {"title": {"displayValue": "Testprodukt"}},
                "offersV2": {"listings": [{"availability": {"type": "OUT_OF_STOCK"}}]},
            }
        )
        self.assertEqual(snapshot["availability_type"], "NO_FEATURED_OFFER")
        self.assertIn("Platzhalter", snapshot["availability_message"])

    def test_out_of_stock_with_offer_data_remains_explicit(self):
        snapshot = app.normalize_item(
            {
                "asin": "B09B2SBHQK",
                "itemInfo": {"title": {"displayValue": "Testprodukt"}},
                "offersV2": {
                    "listings": [
                        {
                            "availability": {"type": "OUT_OF_STOCK"},
                            "merchantInfo": {"name": "Example Seller"},
                        }
                    ]
                },
            }
        )
        self.assertEqual(snapshot["availability_type"], "OUT_OF_STOCK")

    def test_diff_detects_price_and_availability(self):
        old = app.inaccessible_snapshot("B09B2SBHQK", "x", "x")
        old.update(
            accessible=True,
            availability_type="IN_STOCK",
            price_amount=19.99,
            price_currency="EUR",
            price_display="19,99 €",
        )
        new = dict(old)
        new.update(availability_type="OUT_OF_STOCK", price_amount=None, price_display=None)
        changes = app.diff_snapshots(old, new)
        self.assertTrue(any("Verfügbarkeit" in value for value in changes))
        self.assertTrue(any("Preis" in value for value in changes))

    def test_diff_unavailable_is_single_clear_change(self):
        old = app.inaccessible_snapshot("B09B2SBHQK", "x", "x")
        old.update(accessible=True, title="Test", availability_type="IN_STOCK")
        new = app.inaccessible_snapshot("B09B2SBHQK", "ItemNotAccessible", "not accessible")
        changes = app.diff_snapshots(old, new)
        self.assertEqual(len(changes), 1)
        self.assertIn("nicht mehr verfügbar", changes[0])

    def test_client_accepts_items_result_and_maps_item_error(self):
        settings = app.AmazonSettings("id", "secret", "3.2", "tag-21", "www.amazon.de")
        client = app.AmazonCreatorsClient(settings)
        client._access_token = "token"
        client._token_valid_until = time.monotonic() + 600
        response = {
            "errors": [
                {
                    "code": "ItemNotAccessible",
                    "message": "The ItemId B000000002 is not accessible through the Creators API.",
                }
            ],
            "itemsResult": {"items": [{"asin": "B000000001", "itemInfo": {"title": {"displayValue": "One"}}}]},
        }
        with patch.object(app, "request_json", return_value=response):
            snapshots = client.get_items(["B000000001", "B000000002"])
        self.assertTrue(snapshots["B000000001"]["accessible"])
        self.assertFalse(snapshots["B000000002"]["accessible"])
        self.assertEqual(snapshots["B000000002"]["error_code"], "ItemNotAccessible")

    def test_storage_and_page_render_smoke_test(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            original = (app.DATA_DIR, app.CONFIG_FILE, app.STATE_FILE)
            try:
                app.DATA_DIR = Path(directory)
                app.CONFIG_FILE = app.DATA_DIR / "config.json"
                app.STATE_FILE = app.DATA_DIR / "state.json"
                app.ensure_storage()
                page = app.render_page()
                self.assertIn("ASIN Product Watch", page)
                self.assertIn("PRODUCT PRICES AND AVAILABILITY".lower(), page.lower())
                self.assertIn("COMES FROM AMAZON EUROPE CORE", page)
                self.assertIn("ASINs beobachten", page)
                self.assertIn("ASIN-Liste exportieren", page)
                self.assertTrue(app.CONFIG_FILE.exists())
                self.assertTrue(app.STATE_FILE.exists())
            finally:
                app.DATA_DIR, app.CONFIG_FILE, app.STATE_FILE = original

    def test_human_time_uses_berlin_winter_and_summer_time(self):
        with patch.dict(app.os.environ, {"TZ": "Europe/Berlin"}, clear=False):
            self.assertEqual(app.human_time("2026-11-03T21:10:45Z"), "03.11.2026 22:10:45 CET")
            self.assertEqual(app.human_time("2026-07-03T21:10:45Z"), "03.07.2026 23:10:45 CEST")

    def test_expired_product_content_is_removed_but_asin_remains(self):
        state = {
            "products": {
                "B09B2SBHQK": {
                    "snapshot": {"title": "Expired", "image_url": "https://example.test/image.jpg"},
                    "last_confirmed_snapshot": {"title": "Expired"},
                    "last_checked": "2026-11-01T10:00:00Z",
                    "last_confirmed_at": "2026-11-01T10:00:00Z",
                }
            },
            "events": [{"at": "2026-11-01T10:00:00Z", "message": "Expired"}],
            "pending_notifications": [
                {"created_at": "2026-11-01T10:00:00Z", "message": "Expired"}
            ],
        }
        changed = app.purge_expired_product_content(
            state, app.datetime.fromisoformat("2026-11-03T10:00:00+00:00")
        )
        self.assertTrue(changed)
        self.assertIn("B09B2SBHQK", state["products"])
        self.assertNotIn("snapshot", state["products"]["B09B2SBHQK"])
        self.assertNotIn("last_confirmed_snapshot", state["products"]["B09B2SBHQK"])
        self.assertEqual(state["events"], [])
        self.assertEqual(state["pending_notifications"], [])

    def test_product_content_within_24_hours_is_retained(self):
        state = {
            "products": {
                "B09B2SBHQK": {
                    "snapshot": {"title": "Fresh"},
                    "last_checked": "2026-11-03T09:00:01Z",
                }
            },
            "events": [],
            "pending_notifications": [],
        }
        changed = app.purge_expired_product_content(
            state, app.datetime.fromisoformat("2026-11-04T09:00:00+00:00")
        )
        self.assertFalse(changed)
        self.assertEqual(state["products"]["B09B2SBHQK"]["snapshot"]["title"], "Fresh")

    def test_check_baseline_then_price_change_sends_notification(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            original = (app.DATA_DIR, app.CONFIG_FILE, app.STATE_FILE)
            asin = "B09B2SBHQK"
            first = app.inaccessible_snapshot(asin, "", "")
            first.update(
                accessible=True,
                error_code=None,
                error_message=None,
                title="Testprodukt",
                availability_type="IN_STOCK",
                price_amount=19.99,
                price_currency="EUR",
                price_display="19,99 €",
            )
            second = dict(first)
            second.update(price_amount=17.99, price_display="17,99 €")
            environment = {
                "AMAZON_CREDENTIAL_ID": "id",
                "AMAZON_CREDENTIAL_SECRET": "secret",
                "AMAZON_CREDENTIAL_VERSION": "3.2",
                "AMAZON_PARTNER_TAG": "tag-21",
                "AMAZON_MARKETPLACE": "www.amazon.de",
                "PUSHOVER_APP_TOKEN": "token",
                "PUSHOVER_USER_KEY": "user",
            }
            try:
                app.DATA_DIR = Path(directory)
                app.CONFIG_FILE = app.DATA_DIR / "config.json"
                app.STATE_FILE = app.DATA_DIR / "state.json"
                app.ensure_storage()
                config = app.load_config()
                config["asins"] = [asin]
                app.save_config(config)
                with patch.dict(app.os.environ, environment, clear=False), patch.object(
                    app.AmazonCreatorsClient, "get_items", return_value={asin: first}
                ), patch.object(app, "send_pushover") as sender:
                    app.run_check()
                    sender.assert_not_called()
                with patch.dict(app.os.environ, environment, clear=False), patch.object(
                    app.AmazonCreatorsClient, "get_items", return_value={asin: second}
                ), patch.object(app, "send_pushover") as sender:
                    app.run_check()
                    sender.assert_called_once()
                state = app.load_state()
                self.assertEqual(state["pending_notifications"], [])
                self.assertEqual(state["products"][asin]["snapshot"]["price_amount"], 17.99)
                self.assertTrue(any(event.get("kind") == "change" and event.get("notified") for event in state["events"]))
            finally:
                app.DATA_DIR, app.CONFIG_FILE, app.STATE_FILE = original

    def test_missing_offer_alerts_on_second_check_and_recovers(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            original = (app.DATA_DIR, app.CONFIG_FILE, app.STATE_FILE)
            asin = "B09B2SBHQK"
            no_offer = app.normalize_item(
                {"asin": asin, "itemInfo": {"title": {"displayValue": "Testprodukt"}}}
            )
            recovered = app.normalize_item(
                {
                    "asin": asin,
                    "itemInfo": {"title": {"displayValue": "Testprodukt"}},
                    "offersV2": {
                        "listings": [
                            {
                                "isBuyBoxWinner": True,
                                "availability": {"type": "IN_STOCK"},
                                "price": {
                                    "money": {"amount": 12.99, "currency": "EUR", "displayAmount": "12,99 €"}
                                },
                            }
                        ]
                    },
                }
            )
            environment = {
                "AMAZON_CREDENTIAL_ID": "id",
                "AMAZON_CREDENTIAL_SECRET": "secret",
                "AMAZON_CREDENTIAL_VERSION": "3.2",
                "AMAZON_PARTNER_TAG": "tag-21",
                "AMAZON_MARKETPLACE": "www.amazon.de",
                "PUSHOVER_APP_TOKEN": "token",
                "PUSHOVER_USER_KEY": "user",
            }
            try:
                app.DATA_DIR = Path(directory)
                app.CONFIG_FILE = app.DATA_DIR / "config.json"
                app.STATE_FILE = app.DATA_DIR / "state.json"
                app.ensure_storage()
                config = app.load_config()
                config["asins"] = [asin]
                app.save_config(config)

                with patch.dict(app.os.environ, environment, clear=False), patch.object(
                    app.AmazonCreatorsClient, "get_items", return_value={asin: no_offer}
                ), patch.object(app, "send_pushover") as sender:
                    app.run_check()
                    sender.assert_not_called()
                self.assertEqual(app.load_state()["products"][asin]["missing_offer_streak"], 1)

                with patch.dict(app.os.environ, environment, clear=False), patch.object(
                    app.AmazonCreatorsClient, "get_items", return_value={asin: no_offer}
                ), patch.object(app, "send_pushover") as sender:
                    app.run_check()
                    sender.assert_called_once()
                    self.assertIn("kein Featured Offer", sender.call_args.args[1])
                self.assertTrue(app.load_state()["products"][asin]["missing_offer_alerted"])

                with patch.dict(app.os.environ, environment, clear=False), patch.object(
                    app.AmazonCreatorsClient, "get_items", return_value={asin: no_offer}
                ), patch.object(app, "send_pushover") as sender:
                    app.run_check()
                    sender.assert_not_called()

                with patch.dict(app.os.environ, environment, clear=False), patch.object(
                    app.AmazonCreatorsClient, "get_items", return_value={asin: recovered}
                ), patch.object(app, "send_pushover") as sender:
                    app.run_check()
                    sender.assert_called_once()
                    self.assertIn("wieder über die Creators API", sender.call_args.args[1])
                state = app.load_state()
                self.assertEqual(state["products"][asin]["missing_offer_streak"], 0)
                self.assertFalse(state["products"][asin]["missing_offer_alerted"])
            finally:
                app.DATA_DIR, app.CONFIG_FILE, app.STATE_FILE = original


if __name__ == "__main__":
    unittest.main()
