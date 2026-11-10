"""Native folder dialog (tkinter; Windows verified, macOS/Linux unverified) for the 8081 console. A browser never reveals a folder's absolute path, so the local
companion server runs this helper and reads the choice from stdout: one JSON line {"path": "..."} ("" = cancelled)."""
import json
import sys


def main() -> int:
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:          # e.g. Homebrew python on macOS without python-tk: type the path in the console instead
        sys.stderr.write("tkinter unavailable
")
        return 1
    initial = sys.argv[1] if len(sys.argv) > 1 else ""
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)          # the dialog must not open behind the browser
    root.update()
    chosen = filedialog.askdirectory(parent=root, initialdir=initial or None, mustexist=True,
                                     title="AI DEV DIRECTOR - 작업할 폴더 선택")
    root.destroy()
    sys.stdout.buffer.write(json.dumps({"path": chosen or ""}).encode("ascii"))   # ensure_ascii: safe on any codepage
    return 0


if __name__ == "__main__":
    sys.exit(main())
