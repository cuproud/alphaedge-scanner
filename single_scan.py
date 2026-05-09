import sys

def run(symbol):
    print(f"Analyzing: {symbol}")

    # TODO: call your real scanner logic here
    # from scanner import scan
    # scan(symbol)

if __name__ == "__main__":
    symbol = sys.argv[1]
    run(symbol)
