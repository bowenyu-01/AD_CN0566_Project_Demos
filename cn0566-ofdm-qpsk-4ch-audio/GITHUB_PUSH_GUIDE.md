# GitHub publication guide

Review the proposed license, institutional ownership, safety text and sample
audio before publication.

```bash
git init
git branch -M main
git add .
git commit -m "Initial public release: four-channel QPSK-OFDM audio"
git remote add origin git@github.com:YOUR_ACCOUNT/cn0566-ofdm-qpsk-4ch-audio.git
git push -u origin main
git tag -a v2.3.1 -m "v2.3.1 GitHub release"
git push origin v2.3.1
```

Optional GitHub CLI release:

```bash
gh release create v2.3.1 ../cn0566-ofdm-qpsk-4ch-audio-v2.3.1.zip \
  --title "CN0566 four-channel QPSK-OFDM audio v2.3.1" \
  --notes-file GITHUB_RELEASE_NOTES.md
```
