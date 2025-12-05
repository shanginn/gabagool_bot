Here is a clean, minimal, and production-ready `README.md` using `uv`.

***

# 🧠 Gabagool Bot

A high-frequency market-making bot for Polymarket's **15-Minute ETH Up/Down** markets. Built for speed using `uv`, `aiohttp`, and the Polymarket CLOB API.

**⚠️ DISCLAIMER: Use at your own risk. This software is for educational purposes only.**

## ✨ Features

*   **Legging-In Strategy:** Asymmetrically buys YES/NO shares when `Cost(YES) + Cost(NO) < $1.00`.
*   **Imbalance Throttling:** automatically halts trading on one side if exposure becomes too lopsided (`Max Delta > 50`), forcing a hedge.
*   **Auto-Discovery:** Detects and switches to the active 15-minute market window automatically.
*   **Proxy Support:** Native support for Polymarket's Gnosis Safe Proxy architecture.

## 🚀 Setup & Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/yourusername/gabagool-bot.git
    cd gabagool-bot
    ```

2.  **Install dependencies with `uv`:**
    ```bash
    uv add aiohttp python-dotenv py-clob-client rich eth-account eth-utils
    ```

3.  **Configure Credentials:**
    Create a `.env` file in the project root:
    ```ini
    # [REQUIRED] Your Polygon Wallet Private Key
    PRIVATE_KEY=0xYourPrivateKeyHere...

    # [REQUIRED] Your Polymarket Proxy Address
    # Found at: Polymarket.com -> Profile -> Copy Address
    POLYMARKET_PROXY=0xYourProxyAddressHere...
    ```

## 🏃‍♂️ Usage

Start the bot:

```bash
uv run bot.py
```

## 🧠 Strategy Logic

The bot monitors the **Order Book** for price dislocations:

1.  **Entry:** If `Ask(YES) + Ask(NO) < Target (0.985)`, it buys the cheaper side.
2.  **Risk Control:** It tracks the "Delta" (difference between YES/NO shares).
3.  **The Lock:** It prioritizes buying the opposing side to flatten the Delta, locking in a guaranteed USDC profit regardless of the market outcome.

## 📄 License

MIT License.