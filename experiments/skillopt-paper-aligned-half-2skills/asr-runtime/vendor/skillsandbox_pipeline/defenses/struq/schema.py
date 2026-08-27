from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from defenses.shared.common import normalize_title
from defenses.shared.markdown_parser import parse_markdown_blocks
from defenses.shared.schema import BucketPolicy


DOCUMENT_TITLE_BUCKET = "Document Title"
HEADING_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "that",
        "this",
        "your",
        "when",
        "what",
        "why",
        "how",
        "are",
        "read",
        "must",
        "new",
        "via",
        "all",
        "not",
        "use",
    }
)

ALL_COARSE_BLOCK_KINDS = (
    "paragraph",
    "list",
    "checklist",
    "numbered_list",
    "table",
    "fenced_code",
)

PUBLIC_FRONTMATTER_FIELDS = (
    "name",
    "description",
    "metadata",
    "allowed-tools",
    "homepage",
    "license",
    "version",
    "last_updated",
    "self_updating",
)

PUBLIC_BUCKET_POLICIES = (
    BucketPolicy(
        name="Purpose / Overview",
        title_keywords=(
            "overview",
            "purpose",
            "abstract",
            "summary",
            "description",
            "core principles",
            "key principles",
            "principles",
            "core concepts",
            "best practices",
            "the bottom line",
            "role boundary",
            "scope boundaries",
            "what it does",
        ),
        allowed_block_kinds=("paragraph", "list", "checklist", "numbered_list", "table", "fenced_code"),
        paragraph_mode="short",
    ),
    BucketPolicy(
        name="Activation / When to Use",
        title_keywords=(
            "when to activate",
            "when to use",
            "when not to use",
            "activation",
            "use case",
        ),
        allowed_block_kinds=("paragraph", "list", "checklist", "numbered_list"),
        paragraph_mode="short",
    ),
    BucketPolicy(
        name="Inputs / Outputs",
        title_keywords=(
            "inputs",
            "input",
            "outputs",
            "output",
            "arguments",
            "parameters",
            "mandatory output",
            "output format",
        ),
        allowed_block_kinds=("paragraph", "list", "checklist", "numbered_list", "table", "fenced_code"),
        paragraph_mode="short",
    ),
    BucketPolicy(
        name="Usage / Workflow / Steps",
        title_keywords=(
            "workflow",
            "usage",
            "quick start",
            "quick reference",
            "quick commands",
            "common commands",
            "steps",
            "checklist",
            "guide",
            "detailed guide",
            "table of contents",
            "directory structure",
            "common patterns",
            "request patterns",
            "error responses",
            "status codes",
            "caching",
            "rate limiting",
            "pagination",
            "workflow best practices",
            "platform extensions",
            "platform detection",
            "available platform flags",
            "platform specific file structure",
            "file structure",
            "project structure",
            "validation checklist",
            "validation",
            "run all tests",
            "view logs",
            "common workflows",
            "session start",
            "the pipeline",
            "the process",
            "files",
            "testing",
            "testing with coverage",
            "multi environment testing",
            "publishing to pypi",
            "docker image build",
            "status badges",
            "quality gates",
            "implementation pattern",
            "branch naming",
            "branching strategy",
            "git commands",
            "github",
            "pull requests",
            "issues",
            "grove shortcuts",
            "cli lookups",
            "form actions",
            "code organization",
            "naming conventions",
            "key function mappings",
            "standard interface pattern",
            "interface design",
            "concurrency",
            "package organization",
            "domain specific functions",
            "common triggers",
            "task completion",
            "subagent context",
            "exit conditions",
        ),
        allowed_block_kinds=("paragraph", "list", "checklist", "numbered_list", "table", "fenced_code"),
        paragraph_mode="any",
    ),
    BucketPolicy(
        name="Dependencies / Setup",
        title_keywords=(
            "dependencies",
            "setup",
            "installation",
            "requirements",
            "prerequisites",
            "configuration",
            "allowed files",
            "allowed scripts",
            "pyproject toml",
            "env local",
        ),
        allowed_block_kinds=("paragraph", "list", "checklist", "numbered_list", "table", "fenced_code"),
        paragraph_mode="short",
    ),
    BucketPolicy(
        name="Examples",
        title_keywords=(
            "example",
            "examples",
            "your first post",
            "mock",
            "mocking",
            "commit examples",
            "feature",
            "bug fix",
            "breaking change",
            "wrong",
            "correct",
        ),
        allowed_block_kinds=("paragraph", "list", "checklist", "numbered_list", "fenced_code"),
        paragraph_mode="any",
    ),
    BucketPolicy(
        name="Notes / Troubleshooting / Caveats / FAQ",
        title_keywords=(
            "notes",
            "troubleshooting",
            "edge cases",
            "caveats",
            "faq",
            "common pitfalls",
            "anti-patterns",
            "anti patterns",
            "red flags",
            "common rationalizations",
            "platform considerations",
            "tips",
            "safety rules",
            "safety system",
            "security notes",
            "when in doubt",
            "do not do guardrails",
        ),
        allowed_block_kinds=("paragraph", "list", "checklist", "numbered_list", "table", "fenced_code"),
        paragraph_mode="short",
    ),
    BucketPolicy(
        name="Resources / Related Skills",
        title_keywords=(
            "related resources",
            "references",
            "reference",
            "resources",
            "related skills",
            "integration with other skills",
            "see also",
        ),
        allowed_block_kinds=("list", "checklist", "table", "link_paragraph"),
        paragraph_mode="link_only",
    ),
)

PUBLIC_BUCKET_ORDER = [policy.name for policy in PUBLIC_BUCKET_POLICIES]

SKELETON_FRONTMATTER_FIELDS = ("name", "description")

SKELETON_BUCKET_POLICIES = (
    BucketPolicy(
        name="Activation Conditions",
        title_keywords=("when to activate", "activation", "activate this skill"),
        allowed_block_kinds=("list", "checklist", "paragraph"),
        paragraph_mode="short",
    ),
    BucketPolicy(
        name="Core Principles",
        title_keywords=("core principles", "principles", "best practices"),
        allowed_block_kinds=("list", "checklist", "paragraph", "numbered_list"),
        paragraph_mode="short",
    ),
    BucketPolicy(
        name="Constraints",
        title_keywords=(
            "constraints",
            "do",
            "don't",
            "dos and don'ts",
            "api etiquette checklist",
            "safety system",
            "conventional commits format",
            "branching strategy",
            "best practices",
            "what is not touched",
            "anti-patterns",
            "security",
        ),
        allowed_block_kinds=("list", "checklist", "paragraph", "table", "numbered_list", "fenced_code"),
        paragraph_mode="short",
    ),
    BucketPolicy(
        name="Implementation Guidance",
        title_keywords=(
            "setup",
            "request patterns",
            "workflow",
            "usage",
            "execution",
            "implementation",
            "error handling",
            "error responses",
            "pagination",
            "caching",
            "rate limiting",
            "status codes",
            "authentication",
            "loading",
            "quick commands",
            "service overview",
            "tool overview",
            "installation",
            "workflow best practices",
            "troubleshooting",
            "merge conflicts",
            "worktrees",
            "pull requests",
            "issues",
            "workflow runs",
            "api rate limits",
            "project boards",
            "worker secrets",
            "development modes",
            "pages deployment",
            "configuration",
            "commands",
            "creating",
            "storage management",
            "data access",
            "prerequisites",
            "what gets updated",
            "common triggers",
            "testing",
            "coverage",
            "publishing",
            "status badges",
            "when to use",
            "kv storage",
            "r2 storage",
            "d1 database",
            "infra sdk abstraction layer",
            "common patterns",
            "core architecture",
            "file structure",
            "standard interface pattern",
            "domain-specific functions",
            "shortcuts",
            "branch naming",
            "feature branch workflow",
            "session start",
            "preparation",
            "batch issue creation",
            "impact analysis",
            "common workflows",
            "pull latest changes",
            "sync branch with main",
            "save work in progress",
            "undo last commit",
            "formatting",
            "linting",
            "type checking",
            "common type hints",
            "skipping rules",
            "code style principles",
            "clarity over cleverness",
            "meaningful names",
            "early returns",
            "multi-stage build",
            "runtime secrets",
            "volume types",
            "debugging",
            "available hooks",
            "core hooks",
            "language-specific",
            "automation hooks",
            "hook selection",
            "bypassing hooks",
            "basic test structure",
            "table-driven tests",
            "test helpers",
            "mocking with interfaces",
            "benchmarks",
            "test function signatures",
        ),
        allowed_block_kinds=("list", "checklist", "paragraph", "table", "fenced_code", "numbered_list"),
        paragraph_mode="any",
    ),
    BucketPolicy(
        name="Dependencies",
        title_keywords=("dependencies", "allowed files", "allowed scripts", "inputs", "outputs"),
        allowed_block_kinds=("list", "checklist", "table", "fenced_code"),
        paragraph_mode="none",
    ),
    BucketPolicy(
        name="Examples",
        title_keywords=("example", "examples", "commit examples", "feature", "bug fix", "breaking change", "wrong", "correct"),
        allowed_block_kinds=("list", "checklist", "paragraph", "fenced_code", "numbered_list"),
        paragraph_mode="any",
    ),
    BucketPolicy(
        name="Resources",
        title_keywords=("related resources", "references", "see also", "resources"),
        allowed_block_kinds=("list", "checklist", "table", "link_paragraph"),
        paragraph_mode="link_only",
    ),
)

SKELETON_BUCKET_ORDER = [policy.name for policy in SKELETON_BUCKET_POLICIES]


def _policies_for_mode(mode: str) -> tuple[BucketPolicy, ...]:
    return SKELETON_BUCKET_POLICIES if mode == "skeleton" else PUBLIC_BUCKET_POLICIES


def frontmatter_fields(mode: str = "public") -> tuple[str, ...]:
    return SKELETON_FRONTMATTER_FIELDS if mode == "skeleton" else PUBLIC_FRONTMATTER_FIELDS


def bucket_order(mode: str = "public") -> list[str]:
    return SKELETON_BUCKET_ORDER if mode == "skeleton" else PUBLIC_BUCKET_ORDER


def bucket_for_title(title: str | None, mode: str = "public") -> str | None:
    normalized = normalize_title(title)
    title_key = title_signature(title)
    if not title_key:
        return None
    for policy in _policies_for_mode(mode):
        if any(keyword in normalized for keyword in policy.title_keywords):
            return policy.name
    if mode == "public":
        learned_tokens = learned_public_heading_tokens()
        generic_headings = learned_public_generic_headings()
        non_ascii_titles = learned_public_non_ascii_headings()
        if normalized:
            if title and _is_generic_benign_heading(title):
                bucket = fallback_bucket_for_learned_tokens(normalized, learned_tokens)
                if bucket:
                    return bucket
            if title_key in generic_headings:
                return "Usage / Workflow / Steps"
        elif title_key in non_ascii_titles:
            return "Usage / Workflow / Steps"
    return None


def policy_for_bucket(bucket: str | None, mode: str = "public") -> BucketPolicy | None:
    if not bucket:
        return None
    for policy in _policies_for_mode(mode):
        if policy.name == bucket:
            return policy
    return None


@lru_cache(maxsize=1)
def learned_public_heading_tokens() -> frozenset[str]:
    root = Path(__file__).resolve().parents[2] / "benign_skills"
    token_counts: dict[str, int] = {}
    if not root.exists():
        return frozenset()

    for skill_dir in root.iterdir():
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError:
            continue
        for block in parse_markdown_blocks(text):
            if block.kind == "section" and block.level == 2 and block.heading:
                for token in heading_tokens(block.heading):
                    token_counts[token] = token_counts.get(token, 0) + 1
    return frozenset(token for token, count in token_counts.items() if count >= 2)


@lru_cache(maxsize=1)
def learned_public_generic_headings() -> frozenset[str]:
    root = Path(__file__).resolve().parents[2] / "benign_skills"
    headings: set[str] = set()
    if not root.exists():
        return frozenset()

    for skill_dir in root.iterdir():
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError:
            continue
        for block in parse_markdown_blocks(text):
            if block.kind == "section" and block.level == 2 and block.heading:
                if _is_generic_benign_heading(block.heading):
                    sig = title_signature(block.heading)
                    if sig:
                        headings.add(sig)
    return frozenset(headings)


@lru_cache(maxsize=1)
def learned_public_non_ascii_headings() -> frozenset[str]:
    root = Path(__file__).resolve().parents[2] / "benign_skills"
    headings: set[str] = set()
    if not root.exists():
        return frozenset()

    for skill_dir in root.iterdir():
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError:
            continue
        for block in parse_markdown_blocks(text):
            if block.kind == "section" and block.level == 2 and block.heading:
                if not normalize_title(block.heading):
                    sig = title_signature(block.heading)
                    if sig:
                        headings.add(sig)
    return frozenset(headings)


def fallback_bucket_for_learned_tokens(normalized_title: str, learned_tokens: frozenset[str]) -> str | None:
    tokens = [token for token in normalized_title.split() if token in learned_tokens]
    if not tokens:
        return None

    if any(token in tokens for token in ("resource", "resources", "reference", "references", "related", "skills")):
        return "Resources / Related Skills"
    if any(token in tokens for token in ("notes", "troubleshooting", "pitfalls", "anti", "tips", "safety", "security", "issues")):
        return "Notes / Troubleshooting / Caveats / FAQ"
    if any(token in tokens for token in ("example", "examples", "mocking")):
        return "Examples"
    if any(token in tokens for token in ("input", "inputs", "output", "outputs", "arguments", "parameters")):
        return "Inputs / Outputs"
    if any(token in tokens for token in ("setup", "installation", "dependencies", "requirements", "prerequisites", "configuration")):
        return "Dependencies / Setup"
    if any(token in tokens for token in ("activate", "activation")):
        return "Activation / When to Use"
    if any(token in tokens for token in ("overview", "purpose", "abstract", "summary", "principles", "concepts")):
        return "Purpose / Overview"
    if any(
        token in tokens
        for token in (
            "workflow",
            "reference",
            "commands",
            "guide",
            "checklist",
            "process",
            "pipeline",
            "testing",
            "validation",
            "patterns",
            "structure",
            "files",
            "architecture",
            "organization",
            "naming",
            "actions",
            "preparation",
            "conflicts",
            "worktrees",
            "deployment",
            "modes",
            "hooks",
            "helpers",
            "benchmarks",
            "functions",
            "mappings",
            "routes",
            "components",
            "details",
            "phases",
            "steps",
            "commands",
            "responses",
            "types",
            "commits",
            "format",
            "github",
            "branching",
            "branch",
            "error",
        )
    ):
        return "Usage / Workflow / Steps"
    return None


def title_signature(title: str | None) -> str:
    normalized = normalize_title(title)
    if normalized:
        return normalized
    if not title:
        return ""
    return " ".join(title.strip().lower().split())


def heading_tokens(title: str | None) -> tuple[str, ...]:
    normalized = normalize_title(title)
    if not normalized:
        return ()
    tokens = []
    for token in normalized.split():
        if len(token) < 4 or token in HEADING_STOPWORDS:
            continue
        tokens.append(token)
    return tuple(tokens)


def _is_generic_benign_heading(title: str) -> bool:
    stripped = title.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if len(stripped) > 42:
        return False
    if ":" in stripped:
        return False
    if any(ch.isdigit() for ch in stripped[:3]):
        return False
    banned_phrases = (
        "mandatory",
        "required",
        "read this carefully",
        "primary task",
        "does not",
        "read first",
        "should-fix",
    )
    if any(phrase in lowered for phrase in banned_phrases):
        return False
    if stripped.count("(") > 0 and len(stripped) > 32:
        return False
    return True
