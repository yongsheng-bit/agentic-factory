"""Container sandbox: the boundary the denylist never was.

The gates protect the diff; this protects the machine. Everything that
executes repository code or model output - the agent, the test suite,
mutants, the preview renderer, the canary - runs in a disposable
container with exactly one worktree mounted and no route out except the
local LLM through the egress gate. Credentials (gh token, SSH keys, the
sqlite queue, the operator's home) never enter the box: the host owns
commits, pushes and PR creation, so the container's worst day is a bad
diff in a throwaway worktree.

Config (cfg["sandbox"]):
    enabled       flip switch; false keeps every legacy code path intact
    image         docker image with python3 + the repo's test deps
    test_image / preview_image / canary_image   optional per-role overrides
    network       agent network: "factory-llm" with egress, else "none"
    egress        {network, gate_name, listen, target, llm_base}
    limits        {memory, cpus, pids}
    command       inner agent argv; "{prompt_file}" is replaced with the
                  container-side prompt path

Mount hygiene is non-negotiable: never mount the home directory, the
docker socket, or the factory sqlite db into an attempt container. This
module only ever mounts the single per-ticket worktree, the prompt file's
directory, and the factory's scripts/ directory (read-only).
"""

from __future__ import annotations

import os
import subprocess
import uuid

DOCKER = os.environ.get("FACTORY_DOCKER", "docker")
DEFAULT_IMAGE = "factory-agent:local"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK_MOUNT = "/work"
PROMPT_MOUNT = "/prompts"
RUNNER_MOUNT = "/opt/runner"
OUT_MOUNT = "/out"
GATE_SCRIPT = "/opt/factory/egress_gate.py"


def enabled(cfg):
    return bool(((cfg or {}).get("sandbox") or {}).get("enabled"))


def scfg(cfg):
    return (cfg or {}).get("sandbox") or {}


def host_path(path):
    return os.path.abspath(path).replace("\\", "/")


def _limits(cfg):
    lim = scfg(cfg).get("limits") or {}
    return (str(lim.get("memory", "2g")), str(lim.get("cpus", "2")),
            str(lim.get("pids", "256")))


def build_run_argv(image, inner, *, name=None, workdir=None, mounts=(),
                   env=(), network="none", memory="2g", cpus="2", pids="256",
                   read_only=True):
    """Assemble the docker run argv: one worktree, fresh home, no ambient env.

    Env values travel as -e K=V, so callers must only pass values that are
    safe to see in the host process list (LLM coordinates, never tokens).
    """
    argv = [DOCKER, "run", "--rm"]
    if name:
        argv += ["--name", name]
    argv += ["--network", network, "--memory", memory, "--cpus", cpus,
             "--pids-limit", pids]
    if read_only:
        argv += ["--read-only", "--tmpfs", "/tmp"]
    if workdir:
        argv += ["-v", f"{host_path(workdir)}:{WORK_MOUNT}:rw",
                 "-w", WORK_MOUNT]
    for src, dst, mode in mounts:
        argv += ["-v", f"{host_path(src)}:{dst}:{mode}"]
    for key, value in env:
        argv += ["-e", f"{key}={value}"]
    argv += [image] + list(inner)
    return argv


def _docker(args, timeout=120):
    return subprocess.run([DOCKER] + list(args), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def kill(name):
    if not name:
        return
    try:
        _docker(["kill", name], timeout=60)
    except Exception:
        pass


def run_container(cfg, inner, *, workdir=None, mounts=(), env=(),
                  network="none", timeout_s=600, image=None, read_only=True):
    """Run one disposable container. Returns (returncode, combined_output).

    A wall-clock timeout kills the container by name, not just the docker
    client: an orphaned container would keep burning the engine and the
    worktree mount after the attempt was declared dead.
    """
    conf = scfg(cfg)
    name = "factory-" + uuid.uuid4().hex[:12]
    memory, cpus, pids = _limits(cfg)
    argv = build_run_argv(image or conf.get("image") or DEFAULT_IMAGE, inner,
                          name=name, workdir=workdir, mounts=mounts,
                          env=tuple(env.items()) if isinstance(env, dict)
                          else tuple(env),
                          network=network, memory=memory, cpus=cpus,
                          pids=pids, read_only=read_only)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout_s)
    except subprocess.TimeoutExpired:
        kill(name)
        return 124, f"container exceeded {timeout_s}s wall clock and was killed"
    except FileNotFoundError:
        return 127, f"docker command not found: {DOCKER}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def ensure_egress(cfg):
    """Idempotently create the internal network and the one-rule gate.

    The gate container straddles the internal network (where the agent
    containers live, no route to the internet) and the routable bridge
    (where the LLM is reachable). It forwards exactly one TCP port to
    exactly one host:port, so even a fully injected agent has one
    destination and no exfil route.
    """
    conf = scfg(cfg)
    eg = conf.get("egress") or {}
    target = eg.get("target")
    if not target:
        return None
    net = eg.get("network", "factory-llm")
    gate = eg.get("gate_name", "factory-egress")
    listen = str(eg.get("listen", 8080))
    probe = _docker(["inspect", "--format", "{{.State.Running}}", gate])
    if probe.returncode == 0:
        if probe.stdout.strip() == "true":
            return gate
        _docker(["rm", "-f", gate])
    if _docker(["network", "inspect", net]).returncode != 0:
        _docker(["network", "create", "--internal", net])
    _docker(["run", "-d", "--name", gate,
             "--network", net, "--network", "bridge",
             "--memory", "128m", "--cpus", "0.5", "--pids-limit", "64",
             "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL",
             "-e", f"GATE_LISTEN={listen}", "-e", f"GATE_TARGET={target}",
             conf.get("image") or DEFAULT_IMAGE, "python3", GATE_SCRIPT])
    return gate


def wrap_agent_command(cfg, spec, prompt, prompt_path, workdir, env):
    """Turn an agent spec into a docker run invocation.

    Returns (argv, container_name, client_env). The inner command comes
    from sandbox.command (default: the factory's own scripts/local_llm.py
    runner mounted read-only at /opt/runner, so the runner always matches
    the pinned tree). LLM_BASE is rewritten to the egress gate address
    when an egress target is configured.
    """
    conf = scfg(cfg)
    inner = list(conf.get("command") or
                 ["python3", f"{RUNNER_MOUNT}/local_llm.py", "{prompt_file}"])
    container_prompt = f"{PROMPT_MOUNT}/{os.path.basename(prompt_path)}"
    inner = [a.replace("{prompt_file}", container_prompt) for a in inner]
    mounts = [(os.path.dirname(os.path.abspath(prompt_path)),
               PROMPT_MOUNT, "ro"),
              (os.path.join(REPO_ROOT, "scripts"), RUNNER_MOUNT, "ro")]
    eg = conf.get("egress") or {}
    network = conf.get("network") or "none"
    if eg.get("target"):
        ensure_egress(cfg)
        base = eg.get("llm_base")
        if base:
            env = {k: (base if k.upper() == "LLM_BASE" else v)
                   for k, v in env.items()}
    env.pop("PATH", None)
    name = "factory-" + uuid.uuid4().hex[:12]
    memory, cpus, pids = _limits(cfg)
    argv = build_run_argv(conf.get("image") or DEFAULT_IMAGE, inner,
                          name=name, workdir=workdir, mounts=mounts,
                          env=tuple(env.items()), network=network,
                          memory=memory, cpus=cpus, pids=pids)
    return argv, name, env


def run_suite(cfg, command, workdir, timeout_s=600):
    """Run the repo's test command in a no-network container.

    The suite executes repository code, which is exactly the untrusted
    input the sandbox exists for: a poisoned conftest is as dangerous as
    a poisoned prompt.
    """
    conf = scfg(cfg)
    return run_container(cfg, ["sh", "-c", command], workdir=workdir,
                         network="none", timeout_s=timeout_s,
                         image=conf.get("test_image") or None)


def probe(cfg):
    """True when the sandbox image exists and can start a container.

    Callers that would silently misread a docker failure as a test result
    (a 127 exit counted as a killed mutant) must ask first.
    """
    rc, _ = run_container(cfg, ["python3", "--version"], network="none",
                          timeout_s=120)
    return rc == 0
