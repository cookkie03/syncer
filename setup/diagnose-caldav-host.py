"""Run on the NAS host: compare env values privately, then diagnose DSM auth."""
import json
import subprocess
from pathlib import Path

def load_project_env(path):
    # Keep this standalone for NAS hosts whose system Python predates 3.9.
    values = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value.startswith(("'", '"')) and value.endswith(value[:1]):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def main():
    root = Path(__file__).resolve().parent.parent
    expected = load_project_env(root / ".env")
    configuration = subprocess.run(["docker", "compose", "config", "--format", "json"],
                                   cwd=root, capture_output=True, text=True)
    if configuration.returncode:
        print("Impossibile leggere la configurazione Docker Compose")
        return 1
    try:
        configured = json.loads(configuration.stdout)["services"]["caldav-backup"]["environment"]
        matches = []
        for name in ("CALDAV_URL", "CALDAV_USERNAME", "CALDAV_PASSWORD"):
            same = bool(expected.get(name)) and expected[name] == configured.get(name)
            matches.append(same)
            print(f"{name}: .env e Compose {'COINCIDONO' if same else 'DIVERSI O ASSENTI'}")
        if not all(matches):
            print("Login non tentato: correggere prima la lettura della configurazione")
            return 1
        print("Diagnosi DSM: un solo tentativo, senza stampa di password o token", flush=True)
        script = (root / "setup" / "diagnose-caldav-auth.py").read_text()
        result = subprocess.run(["docker", "compose", "run", "--rm", "--no-deps", "-T",
                                 "--entrypoint", "python", "caldav-backup"],
                                cwd=root, input=script, text=True)
        return result.returncode
    except (ValueError, KeyError, TypeError):
        print("Formato configurazione inatteso; nessuna credenziale mostrata")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
