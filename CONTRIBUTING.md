# Contributing

Patches are welcome. Before one can be merged, it needs a sign-off.

## Sign your commits

Every commit must carry a `Signed-off-by` line. By adding it you are
certifying the [Developer Certificate of Origin](https://developercertificate.org)
— in plain terms, that the code is yours to give, that you wrote it or have
the right to submit it, and that you understand it is being contributed under
this project's MIT licence and will be recorded publicly along with your name
and email. This is not paperwork for its own sake: a file-recovery tool that
runs as root on other people's drives has to be able to say where every line
of it came from.

Git will add the line for you:

```bash
git commit -s -m "Your message"
```

It looks like this:

```
Signed-off-by: Your Name <you@example.com>
```

Use your real name and a real address. Pull requests with unsigned commits
will be asked to rebase with `git rebase --signoff`.

## Before you open a pull request

Run the suite. It needs no dependencies, no root and no disk images, and
takes about a minute:

```bash
python3 -m unittest discover -v
```

If you touched a parser or anything under `diskio.py`, read
[SAFETY.md](SAFETY.md) first and re-run the integration tests on Linux:

```bash
RECOVERKIT_INTEGRATION=1 python3 -m unittest discover -v
```

## The one rule that isn't negotiable

**The source drive is never written to.** `diskio.py` is the only module
allowed to open a device, it opens with `os.O_RDONLY` so the kernel enforces
the guarantee rather than our code, and the test suite fails if any other
module grows device-opening code. A change that weakens that is a critical
bug, not a trade-off — however useful the feature attached to it.

Everything else — style, structure, which filesystem to support next — is
open to argument.
