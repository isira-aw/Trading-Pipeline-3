from binance.client import Client
from binance.exceptions import BinanceAPIException
from app.config import settings

class BinanceAPIClient:
    def __init__(self, testnet: bool = True):
        # We enforce testnet by default until stage 3
        self.testnet = testnet
        self.api_key = settings.BINANCE_API_KEY
        self.api_secret = settings.BINANCE_API_SECRET
        
        self.client = Client(
            api_key=self.api_key,
            api_secret=self.api_secret,
            testnet=self.testnet
        )

    def get_historical_klines(self, symbol: str, interval: str, start_str: str, end_str: str = None):
        try:
            return self.client.get_historical_klines(symbol, interval, start_str, end_str)
        except BinanceAPIException as e:
            # Re-raise or handle specific api exceptions
            raise e

    def get_server_time(self):
        return self.client.get_server_time()
