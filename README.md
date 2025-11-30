# Environment

BINANCE_API_KEY=XXX
BINANCE_API_SECRET=XXX

BINANCE_TESTNET=false
TRADING_MODE=live

BASE_CAPITAL=1000
POSITION_SIZE_PCT=0.1

# Setup

# 1. Project dependecies

`pip install -r requirements.txt`

# 2. Run project components

To run all containers in detached mode,
meaning if the terminal is closed they will keep running

`docker compose up -d`

To check docker container logs

`docker compose logs -f bot`
`docker compose logs -f model`

To stop all containers

`docker compose down`