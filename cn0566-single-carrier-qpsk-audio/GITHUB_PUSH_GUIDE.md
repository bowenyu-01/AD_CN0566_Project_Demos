# GitHub publication guide

Review `LICENSE`, `NOTICE.md`, RF safety text and institutional ownership before
publication.

```bash
git init
git branch -M main
git add .
git commit -m "Initial public release: single-carrier QPSK audio"
git remote add origin git@github.com:YOUR_ACCOUNT/cn0566-single-carrier-qpsk-audio.git
git push -u origin main
git tag -a v1.3.0 -m "v1.3.0 GitHub release"
git push origin v1.3.0
```

With GitHub CLI installed, a release archive can be attached with:

```bash
gh release create v1.3.0 ../cn0566-single-carrier-qpsk-audio-v1.3.0.zip \
  --title "CN0566 single-carrier QPSK audio v1.3.0" \
  --notes-file GITHUB_RELEASE_NOTES.md
```
