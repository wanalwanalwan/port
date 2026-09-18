name: Sync crypto prices to Notion

on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:

concurrency:
  group: price-sync
  cancel-in-progress: true

jobs:
  sync:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Sync prices
        env:
          NOTION_TOKEN: ${{ secrets.NOTION_TOKEN }}
          COINGECKO_API_KEY: ${{ secrets.COINGECKO_API_KEY }}
          NOTION_DATA_SOURCE_ID: 181ec133-d228-4a13-8c8b-38e3236edd8f
        run: python sync_prices.py
