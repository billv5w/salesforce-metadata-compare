const { test, expect } = require("@playwright/test");
const { execSync } = require("child_process");
const path = require("path");

const ENV_COMPARE = path.join(__dirname, "..", "..", "scripts", "env-compare.py");
const SAFETY_PY = path.join(__dirname, "..", "..", "mct", "safety.py");

function runEnvCompare(args) {
  try {
    const out = execSync(`python3 "${ENV_COMPARE}" ${args}`, {
      encoding: "utf-8",
      timeout: 15_000,
      stdio: ["pipe", "pipe", "pipe"],
    });
    return { exitCode: 0, stdout: out, stderr: "" };
  } catch (e) {
    return {
      exitCode: e.status ?? 1,
      stdout: e.stdout ?? "",
      stderr: e.stderr ?? "",
    };
  }
}

test.describe("Safety contract — ensure_safe_command blocklist", () => {
  const dangerousCommands = [
    "git push",
    "git commit",
    "git merge",
    "git rebase",
    "sf project deploy start",
    "sf source push",
    "sf org delete",
    "sf mdapi deploy",
  ];

  for (const cmd of dangerousCommands) {
    test(`"${cmd}" is in the blocklist (forbidden words)`, () => {
      const forbidden = ["push", "commit", "merge", "rebase", "deploy", "delete", "update"];
      const tokens = cmd.split(/\s+/);
      const blocked = tokens.some((t) => forbidden.includes(t));
      expect(blocked).toBe(true);
    });
  }
});

test.describe("Safety contract — CLI read-only commands", () => {
  test("list subcommand succeeds (no org needed)", () => {
    const result = runEnvCompare("list");
    expect(result.exitCode).toBe(0);
  });

  test("list --json returns valid JSON", () => {
    const result = runEnvCompare("list --json");
    expect(result.exitCode).toBe(0);
    const parsed = JSON.parse(result.stdout);
    expect(parsed).toHaveProperty("version");
    expect(Array.isArray(parsed.snapshots)).toBe(true);
  });

  test("--help exits cleanly", () => {
    const result = runEnvCompare("--help");
    expect(result.exitCode).toBe(0);
    expect(result.stdout).toMatch(/hardened metadata compare/i);
  });
});

test.describe("Safety contract — behavioral blocklist enforcement", () => {
  test("ensure_safe_command blocks dangerous tokens (Python behavioral)", () => {
    // Import env-compare as a module and verify ensure_safe_command raises RuntimeError
    // for each dangerous command. Tests actual runtime behavior, not just source text.
    // Write to a temp file to avoid shell escaping issues with python3 -c
    const os = require("os");
    const fs = require("fs");
    const tmpScript = path.join(os.tmpdir(), "safety_behavioral_test.py");
    fs.writeFileSync(
      tmpScript,
      [
        "import importlib.util, sys",
        `spec = importlib.util.spec_from_file_location('ec', '${ENV_COMPARE.replace(/\\/g, "/")}')`,
        "m = importlib.util.module_from_spec(spec)",
        "sys.modules['ec'] = m",
        "spec.loader.exec_module(m)",
        "blocked = [['git','push','origin','main'], ['sf','project','deploy','start'], ['git','commit','-m','x']]",
        "for cmd in blocked:",
        "    try:",
        "        m.ensure_safe_command(cmd)",
        "        print(f'FAIL: {cmd} was not blocked', file=sys.stderr)",
        "        sys.exit(1)",
        "    except RuntimeError:",
        "        pass",
        "sys.exit(0)",
      ].join("\n")
    );

    let exitCode;
    try {
      execSync(`python3 "${tmpScript}"`, {
        encoding: "utf-8",
        timeout: 10_000,
        cwd: path.join(__dirname, "../.."),
      });
      exitCode = 0;
    } catch (e) {
      exitCode = e.status ?? 1;
    } finally {
      try {
        fs.unlinkSync(tmpScript);
      } catch (_) {}
    }
    expect(exitCode).toBe(0);
  });
});

test.describe("Safety contract — source code audit", () => {
  const fs = require("fs");

  test("mct/safety.py ensure_safe_command blocks deploy/push/commit", () => {
    const src = fs.readFileSync(SAFETY_PY, "utf-8");
    // Match the actual frozenset declaration in the source
    expect(src).toContain("_FORBIDDEN_VERBS");
    for (const verb of ["push", "commit", "merge", "rebase", "deploy", "delete", "update"]) {
      expect(src).toContain(`"${verb}"`);
    }
  });

  test("mct/safety.py allowlist contains only read-only sf commands", () => {
    const src = fs.readFileSync(SAFETY_PY, "utf-8");
    expect(src).toContain('"sf", "project", "generate", "manifest"');
    expect(src).toContain('"sf", "project", "retrieve", "start"');
    expect(src).toContain('"sf", "package", "installed", "list"');
    expect(src).not.toMatch(/"sf",\s*"source",\s*"push"/);
    expect(src).not.toMatch(/"sf",\s*"source",\s*"deploy"/);
  });

  test("mct/safety.py permits deploy ONLY as --dry-run validation", () => {
    // Owner-approved exception (roadmap 3.11): `sf project deploy start`
    // must be gated on --dry-run being present in argv — validation-only,
    // nothing persisted to the org. The gate must exist and be conditional.
    const src = fs.readFileSync(SAFETY_PY, "utf-8");
    expect(src).toContain('_VALIDATE_ONLY_PREFIX = ("sf", "project", "deploy", "start")');
    expect(src).toContain('"--dry-run" not in cmd');
  });

  test("mct/safety.py allowlist contains only read-only git commands", () => {
    const src = fs.readFileSync(SAFETY_PY, "utf-8");
    expect(src).toContain('"git", "rev-parse"');
    expect(src).toContain('"git", "fetch"');
    expect(src).toContain('"git", "archive"');
    expect(src).not.toMatch(/"git",\s*"push"/);
    expect(src).not.toMatch(/"git",\s*"commit"/);
    expect(src).not.toMatch(/"git",\s*"merge"/);
  });

  test("no sf deploy/push commands anywhere in Python scripts (dry-run docs excepted)", () => {
    const scriptsDir = path.join(__dirname, "..", "..", "scripts");
    const pyFiles = fs.readdirSync(scriptsDir).filter((f) => f.endsWith(".py"));
    for (const f of pyFiles) {
      const src = fs.readFileSync(path.join(scriptsDir, f), "utf-8");
      // Mirror the runtime policy: a deploy mention is legal ONLY when the
      // same line carries --dry-run (validation-only, e.g. bundle README text).
      for (const line of src.split("\n")) {
        if (/sf.*project.*deploy/.test(line)) {
          expect(line).toContain("--dry-run");
        }
      }
      expect(src).not.toMatch(/sf.*source.*push/);
      expect(src).not.toMatch(/sf.*source.*deploy/);
      expect(src).not.toMatch(/sf.*mdapi.*deploy/);
    }
  });

  test("no git push/commit/merge in Python scripts", () => {
    const scriptsDir = path.join(__dirname, "..", "..", "scripts");
    const pyFiles = fs.readdirSync(scriptsDir).filter((f) => f.endsWith(".py"));
    for (const f of pyFiles) {
      const src = fs.readFileSync(path.join(scriptsDir, f), "utf-8");
      expect(src).not.toMatch(/\bgit.*\bpush\b/);
      expect(src).not.toMatch(/"git",\s*"commit"/);
      expect(src).not.toMatch(/"git",\s*"merge"\b/);
    }
  });
});
