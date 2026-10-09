# Contributing

Thanks for helping. Bug reports, translations and fixes are all welcome.

- **Bugs and ideas:** use the [issue forms](https://github.com/roko-tech/Anime-Subs-Customizer/issues/new/choose). Logs contain your folder and file names: remove anything private before attaching one.
- **Security problems:** please don't open a public issue; see [SECURITY.md](SECURITY.md).
- **Arabic wording:** corrections to the Arabic interface or documentation are very welcome. The strings are in `anime_subs/i18n.py`; every key exists in both English and Arabic (a test checks this).

## Code changes

1. Read [docs/development.md](docs/development.md) to run the app and the tests from source, and [docs/architecture.md](docs/architecture.md) for how it fits together.
2. **Never test against your real library or settings.** Use `tools/make_demo_library.py` for test data and set `ASC_INSTANCE_TOKEN` to run a test copy next to your real one.
3. Keep changes small and focused. Fix bugs test-first: add a test that fails, then make it pass.
4. The styling engine must stay byte-faithful (encoding, line endings, `[Fonts]`, extradata) and must never lose an original. Changes there need tests for the round trip and the backup rules.
5. New user-visible text goes into both string tables, and the layout must still mirror correctly in Arabic.
6. Run `python -m pytest tests -q` before you open a pull request.

By contributing you agree that your contribution is licensed under the project's [MIT licence](LICENSE).
