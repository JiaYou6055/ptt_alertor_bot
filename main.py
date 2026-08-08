import sys
from bot import main

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 PTT Alert Bot 已停止運行。")
        sys.exit(0)
