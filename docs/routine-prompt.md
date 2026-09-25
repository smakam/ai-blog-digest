Run the AI Blog Digest daily job in this repository (smakam/ai-blog-digest). Follow these steps
exactly. Do not modify any code, config, or the feed list, and do not try to fix failures.
This routine never pushes to `main`; its state and logs live on the branch `claude/digest-routine`.

1. Switch to the routine's branch and bring it up to date with main:
   `git fetch origin`
   then, if `origin/claude/digest-routine` exists:
   `git checkout -B claude/digest-routine origin/claude/digest-routine && git merge --no-edit origin/main`
   otherwise (first run only):
   `git checkout -B claude/digest-routine origin/main`
2. Install: `pip install -q -e .`
3. Run the digest:
   `DIGEST_OPTION=routine python -m digest`
   It sends the digest and any error notices to Telegram by itself. Do not send Telegram messages
   yourself, and never print, echo, or inspect environment variables or secrets.
4. Whatever the exit code, commit the changes under `state/` and `logs/`:
   `git add state logs && git commit -m "digest: state and logs $(date -u +%F) (routine)"`
   (skip the commit if there is nothing to commit), then `git push origin claude/digest-routine`.
5. Finish with a short report: the digest command's exit code, its last 15 lines of output, and
   whether the push succeeded. If anything failed, report it; do not attempt a fix.
