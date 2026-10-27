"""Stable dashboard CLI; launch policy and HTTP views live in feature folders."""
import sys

if __name__ == '__main__':
    if '--build-only' in sys.argv:
        from web.server import main
    else:
        from launch.startup import main
    main()
