import sys
import os
from dotenv import load_dotenv

load_dotenv()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "cli"

    if mode == "web":
        from src.web import launch_web
        port = int(os.getenv("GRADIO_PORT", "7860"))
        share = os.getenv("GRADIO_SHARE", "false").lower() == "true"
        print(f"\nWeb UI → http://localhost:{port}")
        print(f"Gitea  → http://localhost:{os.getenv('GITEA_PORT', '3000')}\n")
        launch_web(port=port, share=share)
    elif mode == "test":
        import subprocess
        sys.exit(subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short"],
        ).returncode)
    else:
        from src.cli import app
        if mode == "cli":
            sys.argv.pop(1)
        app()


if __name__ == "__main__":
    main()
