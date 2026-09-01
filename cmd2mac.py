#!/usr/bin/env python3
r"""
Rewrite an Etsy-order build command so it runs on macOS.

The web console writes the copied command with a bare `python` prefix, which is
right for the Windows box the shop normally prints from. macOS usually has only
`python3` on PATH, so the pasted command dies with "command not found" before it
starts. This swaps the interpreter and leaves every build flag untouched.

    # straight from an argument
    python3 cmd2mac.py "python map2model.py --bbox -79.40 43.63 -79.37 43.66 --size 200 -o toronto.3mf"

    # from a pipe or a paste (Ctrl-D to end)
    pbpaste | python3 cmd2mac.py

    # read the clipboard and write the converted command straight back to it
    python3 cmd2mac.py --clip

    # convert, then run it here
    python3 cmd2mac.py --run "python map2model.py --bbox ... -o toronto.3mf"

Only the interpreter prefix changes. `py`, `py -3`, `python.exe`, a full
`C:\...\python.exe` path, a PowerShell `& "..."` call and a leading `.\` on the
script name all collapse to `python3 map2model.py`; everything after
`map2model.py` is passed through byte for byte, including `--filaments` and the
`-o` name. A shell prompt, a `\`- or `^`-continued multi-line command, and any
prose pasted around it from the order are all handled.

It does NOT touch argument values -- a hand-written Windows path in `-o`
(`-o out\city.3mf`) keeps its backslashes. The console never emits one; it
hands `run()` a bare name and lets `model_out_path()` route it.
"""
import subprocess
import sys

SCRIPT = "map2model.py"


def to_mac(text):
    """Pull the map2model command out of `text` and put `python3` in front."""
    lines = text.splitlines() or [text]
    start = next((i for i, ln in enumerate(lines) if SCRIPT in ln), None)
    if start is None:
        raise ValueError(f"no {SCRIPT!r} command found in the input")

    # Collect the command, following `\` (POSIX) or `^` (cmd) line
    # continuations and stopping at the first line that has neither -- so
    # trailing prose from a pasted order is left behind.
    parts = []
    for ln in lines[start:]:
        stripped = ln.rstrip()
        cont = stripped.endswith(("\\", "^"))
        parts.append(stripped[:-1].strip() if cont else stripped.strip())
        if not cont:
            break
    line = " ".join(p for p in parts if p)

    # Everything up to and including the script name is the interpreter
    # invocation, and it is the only part that differs between platforms.
    _, _, tail = line.partition(SCRIPT)
    return f"python3 {SCRIPT}{tail}".rstrip()


SELFTEST_CASES = [
    ("python map2model.py --bbox 1 2 3 4 -o x.3mf",
     "python3 map2model.py --bbox 1 2 3 4 -o x.3mf"),
    ("python3 map2model.py --size 150",
     "python3 map2model.py --size 150"),
    ("py -3 map2model.py --size 200",
     "python3 map2model.py --size 200"),
    ("py map2model.py --split",
     "python3 map2model.py --split"),
    (r"C:\Python39\python.exe map2model.py --frame",
     "python3 map2model.py --frame"),
    (r"python .\map2model.py --box",
     "python3 map2model.py --box"),
    (r'& "C:\Program Files\Python\python.exe" map2model.py --roofs all',
     "python3 map2model.py --roofs all"),
    ("PS C:\\shop> python map2model.py --lod 1",
     "python3 map2model.py --lod 1"),
    ("Here is your order:\n\npython map2model.py --bbox 1 2 3 4\n\nthanks!",
     "python3 map2model.py --bbox 1 2 3 4"),
    ("python map2model.py --bbox 1 2 3 4 \\\n  --size 200 \\\n  -o city.3mf",
     "python3 map2model.py --bbox 1 2 3 4 --size 200 -o city.3mf"),
    ("python map2model.py --split ^\n  --box",
     "python3 map2model.py --split --box"),
    ("  python   map2model.py   --filaments terrain=matte_grass_green,roads=basic_black  ",
     "python3 map2model.py   --filaments terrain=matte_grass_green,roads=basic_black"),
]


def selftest():
    bad = 0
    for src, want in SELFTEST_CASES:
        try:
            got = to_mac(src)
        except ValueError as e:
            got = f"<error: {e}>"
        ok = got == want
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {src!r}")
        print(f"         -> {got!r}")
        if not ok:
            print(f"         want {want!r}")
    print(f"\n{len(SELFTEST_CASES) - bad}/{len(SELFTEST_CASES)} passed")
    return 1 if bad else 0


def main(argv):
    args = argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__.strip())
        return 0
    if "--selftest" in args:
        return selftest()

    do_clip = "--clip" in args
    do_run = "--run" in args
    rest = [a for a in args if a not in ("--clip", "--run")]

    if do_clip:
        src = subprocess.run(["pbpaste"], capture_output=True,
                             text=True).stdout
    elif rest:
        src = " ".join(rest)
    else:
        src = sys.stdin.read()

    try:
        cmd = to_mac(src)
    except ValueError as e:
        print(f"cmd2mac: {e}", file=sys.stderr)
        return 2

    print(cmd)

    if do_clip:
        subprocess.run(["pbcopy"], input=cmd, text=True)
        print("(written back to the clipboard)", file=sys.stderr)

    if do_run:
        import shlex
        return subprocess.run(shlex.split(cmd)).returncode

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
