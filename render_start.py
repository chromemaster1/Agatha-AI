"""Render web-service entry point for Agatha's hosted dashboard."""
import os
import shutil
import subprocess


def provision_owner() -> None:
    import tools_auth

    if tools_auth.account_exists():
        return

    name = os.getenv("AGATHA_OWNER_NAME", "Owner").strip() or "Owner"
    identifier = os.getenv("AGATHA_OWNER_IDENTIFIER", "").strip()
    password = os.getenv("AGATHA_OWNER_PASSWORD", "")
    if not identifier or len(password) < 16:
        raise RuntimeError(
            "Set AGATHA_OWNER_IDENTIFIER and a 16+ character AGATHA_OWNER_PASSWORD "
            "in Render environment variables before the first deployment."
        )

    _, account = tools_auth.signup(name, identifier, password)
    try:
        import tools_profile
        tools_profile.set_name(account["name"])
    except Exception as error:
        print(f"[Warning] Could not initialize the owner profile: {error}")


def start_signature_refresh() -> None:
    freshclam = shutil.which("freshclam")
    if not freshclam:
        return
    try:
        subprocess.Popen(
            [freshclam, "--quiet"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        print(f"[Warning] Could not start ClamAV signature refresh: {error}")


def main() -> None:
    if os.getenv("AGATHA_HOSTED_MODE", "").strip().lower() not in ("1", "true", "yes", "on"):
        raise RuntimeError("render_start.py is for hosted deployments; set AGATHA_HOSTED_MODE=true.")

    data_dir = os.getenv("AGATHA_DATA_DIR", "").strip()
    if not data_dir:
        raise RuntimeError("Set AGATHA_DATA_DIR to the mounted persistent disk path, such as /var/data.")
    os.makedirs(data_dir, exist_ok=True)
    if not os.access(data_dir, os.W_OK):
        raise RuntimeError(f"AGATHA_DATA_DIR is not writable: {data_dir}")

    provision_owner()
    start_signature_refresh()

    from tools_instance import acquire_instance_lock
    acquired, message = acquire_instance_lock()
    if not acquired:
        raise RuntimeError(message)

    import main_agent
    main_agent.run_agent_loop()


if __name__ == "__main__":
    main()
