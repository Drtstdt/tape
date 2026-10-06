import csv
import json
import requests

# 1. Sprawdzony i działający punkt końcowy API
API_URL = "https://streaming.bitquery.io/graphql"

# 2. Wklej tutaj swój token autoryzacyjny
API_TOKEN = __import__("os").environ.get("BITQUERY_TOKEN", "")  # token removed from the repo; set BITQUERY_TOKEN

# 3. POPRAWIONE zapytanie GraphQL (Pola wyciągnięte bezpośrednio z kostki Trades)
query = """
query GetTrades {

  Trading {

    Trades(
      where: {
  Pair: {
    Market: {
      Network: {
        is: "Solana"
      }
    }
  }

  Block: {
    Date: {
      before_relative: {
        days_ago: 25
      }

      after_relative: {
        days_ago: 27
      }
    }
  }
}

      orderBy: {
        descending: Block_Time
      }

      limit: {
        count: 20
      }
    ) {

      Block {
        Date
        Time
      }

      Side

      Amounts {
        Base
        Quote
      }

      AmountsInUsd {
        Base
        Quote
      }

      Price

      Pair {
        Currency {
          Symbol
        }

        QuoteCurrency {
          Symbol
        }
      }
    }
  }
}
"""

headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_TOKEN}"
}

try:
    print(f"Wysyłam zapytanie do: {API_URL}...")

    response = requests.post(
        API_URL,
        json={"query": query},
        headers=headers,
        timeout=60
    )

    response.raise_for_status()

    response_data = response.json()

    if "errors" in response_data:
        print("\n[BŁĄD BITQUERY]")
        print(
            json.dumps(
                response_data["errors"],
                indent=2,
                ensure_ascii=False
            )
        )

    else:
        trades = (
            response_data
            .get("data", {})
            .get("Trading", {})
            .get("Trades", [])
        )

        if not trades:
            print("\nBrak trade'ów.")
            print(json.dumps(response_data, indent=2))

        else:

            csv_filename = "bitquery_30_days_back.csv"

            with open(
                csv_filename,
                mode="w",
                newline="",
                encoding="utf-8"
            ) as csv_file:

                fieldnames = [
                    "Data",
                    "Czas",
                    "Side",
                    "BaseToken",
                    "BaseAddress",
                    "QuoteToken",
                    "QuoteAddress",
                    "BaseAmount",
                    "QuoteAmount",
                    "BaseUSD",
                    "QuoteUSD",
                    "Price",
                    "Trader",
                    "TxHash"
                ]

                writer = csv.DictWriter(
                    csv_file,
                    fieldnames=fieldnames
                )

                writer.writeheader()

                for item in trades:

                    block = item.get("Block") or {}
                    amounts = item.get("Amounts") or {}
                    amounts_usd = item.get("AmountsInUsd") or {}
                    pair = item.get("Pair") or {}
                    base = pair.get("BaseCurrency") or {}
                    quote = pair.get("QuoteCurrency") or {}
                    tx = item.get("TransactionHeader") or {}
                    trader = item.get("Trader") or {}

                    writer.writerow({
                        "Data": block.get("Date"),
                        "Czas": block.get("Time"),

                        "Side": item.get("Side"),

                        "BaseToken": base.get("Symbol"),
                        "BaseAddress": base.get("Address"),

                        "QuoteToken": quote.get("Symbol"),
                        "QuoteAddress": quote.get("Address"),

                        "BaseAmount": amounts.get("Base"),
                        "QuoteAmount": amounts.get("Quote"),

                        "BaseUSD": amounts_usd.get("Base"),
                        "QuoteUSD": amounts_usd.get("Quote"),

                        "Price": item.get("Price"),

                        "Trader": trader.get("Address"),

                        "TxHash": tx.get("Hash")
                    })

            print(
                f"\nSukces! Pobrano {len(trades)} trade'ów."
            )

            print(
                f"CSV: {csv_filename}"
            )

except requests.exceptions.RequestException as e:
    print(f"\nBłąd HTTP/API: {e}")

except Exception as e:
    print(f"\nNieoczekiwany błąd: {e}")
