# Vendored from agentic-coding/scripts/review_html at
# 5d07d2f7f31675709f57b35579f72058e846fa67.
# Upstream declares no licence; see UPSTREAM.txt for source and modifications.
"""Top-level page template.

Every placeholder is a substituted value; the stylesheet comes from
``css.CSS`` through the ``$css`` placeholder."""

from __future__ import annotations

from string import Template

PAGE_TEMPLATE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>$title_plain</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
$publish_metadata
<style>$css</style>
</head>
<body>
<header class="top-bar">
  <div class="top-stripe"></div>
  <div class="top-content">
    <span class="repo-title">$repo_name</span>
    $metrics_chips
  </div>
</header>

<div class="page">
  <h1>$title_html</h1>
  <p class="muted">$subtitle</p>

  <section class="card-grid">
    $at_a_glance
    $important_links
    $verdict_card
    $findings_summary$tests_card
  </section>

  $toc

  $pr_description_section
  $commits_section
  $explanation_section
  $important_changes_section
  $decisions_section
  $findings_section$tests_section
  $unresolved_comments_section$diagram_section
  $files_section
  $double_check_section

  <footer>
    Generated $timestamp · Dream <code>$repo_path</code> · inspect with <code>dream status</code>.
  </footer>
</div>
</body>
</html>
""")
