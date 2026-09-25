Run the AI Blog Digest daily job in this repository. Follow these steps exactly and do not modify
any code, config, or feed list.

1. If `python -c "import digest"` fails, run `pip install -e .`.
2. Run `python -m digest`. It sends the digest and any error notices to Telegram itself.
   Do not send Telegram messages yourself and do not print or echo any environment variable.
3. Whatever the exit code, commit the changes under `state/` and `logs/` with the message
   `digest: state and logs <YYYY-MM-DD> (<DIGEST_OPTION>)` and push to the current branch. If the
   push is rejected, `git pull --rebase` and push again (up to 3 times).
4. Report the exit code and the last 20 lines of the command's output. If the exit code was
   non-zero, do not try to fix anything; just report.
