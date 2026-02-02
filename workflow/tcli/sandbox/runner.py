"""Self-correcting test runner with structured reporting."""
import importlib
import sys
import time
import traceback

# ANSI
G, R, Y, C, RST = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[0m"
B = "\033[1m"


class SandboxRunner:
    def __init__(self, base_url="http://localhost:5001"):
        self.base_url = base_url
        self.results: list[tuple[str, str, str | None]] = []
        self.start_time = 0.0

    @property
    def all_passed(self) -> bool:
        return all(r[0] == "PASS" for r in self.results)

    def test(self, name: str, fn):
        """Run a single test, catch errors, log result."""
        try:
            fn()
            self.results.append(("PASS", name, None))
            sys.stdout.write(f"  {G}PASS{RST} {name}\n")
        except AssertionError as e:
            self.results.append(("FAIL", name, str(e)))
            sys.stdout.write(f"  {R}FAIL{RST} {name}: {e}\n")
        except Exception as e:
            tb = traceback.format_exc().splitlines()[-3:]
            self.results.append(("ERROR", name, str(e)))
            sys.stdout.write(f"  {Y}ERROR{RST} {name}: {e}\n")
            for line in tb:
                sys.stdout.write(f"        {line}\n")

    def run(self, modules: list[str]):
        """Import and execute test modules."""
        self.start_time = time.time()
        sys.stdout.write(f"\n{B}{'='*60}{RST}\n")
        sys.stdout.write(f"{B}  TC Sandbox Test Runner{RST}\n")
        sys.stdout.write(f"{B}  Base URL: {self.base_url}{RST}\n")
        sys.stdout.write(f"{B}{'='*60}{RST}\n\n")

        for mod_name in modules:
            full = f"tcli.sandbox.test_{mod_name}"
            sys.stdout.write(f"{C}--- {mod_name} ---{RST}\n")
            try:
                mod = importlib.import_module(full)
                if hasattr(mod, "register"):
                    mod.register(self)
                else:
                    sys.stdout.write(f"  {Y}SKIP{RST} no register() in {full}\n")
            except ImportError as e:
                sys.stdout.write(f"  {R}IMPORT ERROR{RST} {full}: {e}\n")
                self.results.append(("ERROR", f"import:{mod_name}", str(e)))
            sys.stdout.write("\n")

    def report(self):
        """Print summary."""
        elapsed = time.time() - self.start_time
        total = len(self.results)
        passed = sum(1 for r in self.results if r[0] == "PASS")
        failed = sum(1 for r in self.results if r[0] == "FAIL")
        errors = sum(1 for r in self.results if r[0] == "ERROR")

        sys.stdout.write(f"{B}{'='*60}{RST}\n")
        sys.stdout.write(f"  {B}Results:{RST}  ")
        sys.stdout.write(f"{G}{passed} passed{RST}  ")
        if failed:
            sys.stdout.write(f"{R}{failed} failed{RST}  ")
        if errors:
            sys.stdout.write(f"{Y}{errors} errors{RST}  ")
        sys.stdout.write(f"/ {total} total  ({elapsed:.1f}s)\n")
        sys.stdout.write(f"{B}{'='*60}{RST}\n")

        if failed or errors:
            sys.stdout.write(f"\n{R}Failures:{RST}\n")
            for status, name, detail in self.results:
                if status != "PASS":
                    sys.stdout.write(f"  {R}{status}{RST} {name}\n")
                    if detail:
                        sys.stdout.write(f"         {detail}\n")
            sys.stdout.write("\n")
