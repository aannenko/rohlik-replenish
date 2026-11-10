# rohlik-replenish

An agent skill that fills your [Rohlik.cz](https://www.rohlik.cz) cart for the next delivery from your own order
history, without asking what to buy. It learns how fast your household uses each kind of product, adds what will run
out before the delivery after next, and can stock up on long-life staples when they're on a deep sale. You review the
cart and check out in the Rohlik app or website: the skill never places, changes or cancels an order.

**Rohlik.cz (Czech Republic) only.** It is tested there, and it talks to Rohlik.cz's MCP server and prices in CZK.

## Requirements

- A Rohlik.cz account with at least 3 delivered orders in the last 6 months. The skill learns from the last 12 months.
- An agent that can run shell commands and load skills: GitHub Copilot CLI or Claude Code, which install it as a
  plugin, or any agent that reads [Agent Skills](https://agentskills.io).
- [uv](https://docs.astral.sh/uv/) 0.5.20 or newer, which provides Python and the packages. If it's missing, the
  agent offers to install it.
- Windows, macOS or Linux with the system keyring (on Linux, a Secret Service such as GNOME Keyring or KWallet). Used
  live on Windows; the tests run on all three.

## Install

GitHub Copilot CLI:

```
copilot plugin marketplace add aannenko/rohlik-replenish
copilot plugin install rohlik-replenish@rohlik-replenish
```

Claude Code:

```
/plugin marketplace add aannenko/rohlik-replenish
/plugin install rohlik-replenish@rohlik-replenish
```

Tested with GitHub Copilot CLI. The Claude Code commands follow Claude Code's documentation but haven't been tried yet.

Other agents: copy [`skills/rohlik-replenish`](skills/rohlik-replenish) into the agent's skills folder. Install it
only once: every copy shares the same data, and in Copilot CLI a copied skill hides the plugin's.

## Use

Ask your agent to fill your Rohlik cart. The first time:

1. You log in to Rohlik in your browser. Later runs renew the login by themselves; when one can't, the agent asks you
   to log in again.
2. The agent sorts the products you've bought into tags, each a need such as "milk 1.5%".
3. The first run is a dry run: it writes nothing to the cart and shows a setup report with the cart it would build and
   the tags to check. Correct what's wrong, or say it's fine, and the agent runs again to fill the cart.

After that, each request fills the cart. Check the counts before you check out: a count you change is how the skill
learns your amounts. You can also tell the agent things like "never order X", "only buy X on sale" or "P is my
preferred X".

## Updates

```
copilot plugin update rohlik-replenish@rohlik-replenish
claude plugin update rohlik-replenish@rohlik-replenish
```

Your data lives outside the plugin, so updating or reinstalling it keeps your tags and history. When a release's notes
say it upgrades the database, back the database up before you use the new version: its first command upgrades it, and
an upgraded database can't go back to an older version (see [Upgrading](skills/rohlik-replenish/README.md#upgrading)).

## Privacy and safety

- The engine can call only six read tools and two cart-write tools of Rohlik's MCP server, and `SKILL.md` forbids the
  agent to check out, place, change or cancel orders, reserve delivery slots or change checkout settings.
- The engine keeps its database on your computer, one SQLite file per Rohlik account in your user data folder with no
  addresses or payment data, and the login tokens in the system keyring. What it prints is read by your agent, so,
  like the rest of your conversation, it reaches your agent's model provider, whose data policies apply.
- This project is not affiliated with or endorsed by Rohlik.

## Documentation

- [`SKILL.md`](skills/rohlik-replenish/SKILL.md): the agent's instructions.
- [The skill's README](skills/rohlik-replenish/README.md): commands, the data folder, upgrading and development.
- [`docs/spec.md`](docs/spec.md): the specification.

## Development

From `skills/rohlik-replenish`:

```
uv run --with pytest --with-requirements replenish.requirements.txt pytest
```

CI runs the tests on Linux, macOS and Windows, starts the engine the way an installed plugin does, and checks the
lockfiles. [`AGENTS.md`](AGENTS.md) has the rules for working in this repository.

## License

[MIT](LICENSE)
