import argparse
import uvicorn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    args = parser.parse_args()
    uvicorn.run("control_plane.main:app", host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
