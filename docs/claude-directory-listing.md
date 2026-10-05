# Claude Connectors Directory: submission pack

Who can submit: any Pro or Max account (submit from your own account, no role or company check).
Free accounts cannot. Portal: https://claude.ai/directory/manage, then **Submit new**, then **MCP connector**.
After submitting, an automated policy scan runs and the connector is listed as **Community** by default.
Reviewer feedback, if any, appears on the submission's page in the portal. Escalations: mcp-review@anthropic.com.

Rules: https://claude.com/docs/connectors/building/submission and
https://claude.com/docs/connectors/building/review-criteria

## Before you open the portal (one time, about 20 minutes)

1. **Reviewer login.** In a private browser window, go to https://milatexai.com, choose Sign in, and create an
   account with a dedicated address (for example `yasaminfayyaz+review@gmail.com`) and a password. Reviewers need
   email and password; a magic link or Google sign-in would not work for them. If the sign-in page offers no
   password option, turn on Email and password under Authentication in the WorkOS dashboard first.
2. **Tell Claude Code it exists.** It sets that account to Pro in our database, so every tool (including the Pro
   figure tools) works for reviewers.
3. **Reviewer token.** The sample paper is ready in a private repo:
   https://github.com/yasaminfayyaz/milatexai-review-paper. In GitHub: Settings, Developer settings, Personal access
   tokens, Fine-grained tokens, Generate new token. Repository access: only `milatexai-review-paper`. Permissions:
   Contents, Read and write. Expiration: 90 days or more.
4. **Connect it once.** In Claude, signed in to the reviewer account on milatexai.com's connector, ask "connect my
   project", open the link, paste `https://github.com/yasaminfayyaz/milatexai-review-paper.git` and the token.
   Then ask "list my projects" to check it works.
5. Wait until the build with the tool titles is live (Claude Code confirms it).

## Step: Connection

Server URL: `https://milatexai.com/mcp` (single URL).

## Step: Tools

Synced automatically. Every tool now has a title and a read-only or destructive annotation. Nothing should be flagged.

## Step: Listing

- **Name**: MiLatexAI for Overleaf and Git
- **One-liner**: Edit your Overleaf or Git LaTeX project from Claude. Every change is a real Git commit you can review and undo. No downloads, no copy-paste.
- **Description**:

  MiLatexAI connects Claude to your LaTeX project so you can work on your paper by talking to it.
  Ask Claude to read a section, rewrite a paragraph, fix a compile error, add a citation or tidy a
  bibliography, and the change lands in your Overleaf project or Git repository.

  What it does
  - Reads and edits your real files in Overleaf (through its Git integration) or in GitHub, GitLab,
    Bitbucket and self-hosted Git repositories.
  - Every edit is a Git commit, so you can see the diff, restore any file, or roll back to a checkpoint.
  - Shows Claude the compiled page, tables and figures as images, so it can check layout and not only code.
  - Checks that the paper compiles and points at the failing line when it does not.
  - Adds citations from a DOI or arXiv id by fetching the real BibTeX, so references are never invented.
  - Keeps your access token out of the chat: you paste it into a secure web form, it is stored
    encrypted, and it is never written into the conversation.

  What it does not do
  - It only touches the projects you connect. It does not read the rest of your account.
  - It does not keep your documents. Files are read and written while a request runs.

  Plans
  Reads are always free and unlimited. The free plan includes 10 write commits per month. Pro is
  unlimited. Subscribing happens on Stripe's own checkout page; the connector never charges or
  handles card details. The source code is public under the AGPL license.

  Before you start
  Overleaf's Git integration is a paid Overleaf feature (many universities include it). GitHub,
  GitLab and Bitbucket repositories only need a free access token.

- **Categories**: whichever of these the portal offers: Productivity, Writing, Developer tools, Education, Research
- **Documentation URL**: https://milatexai.com/ (the "Add MiLatexAI in two minutes" section)
- **Privacy policy URL**: https://milatexai.com/privacy
- **Support contact**: support@milatexai.com
- **Icon**: `docs/directory/icon-512.png` (a 64 px version is next to it)
- **Slug** (permanent): `milatexai`

## Step: Use cases

- Edit a paper in Overleaf or Git by conversation.
- Fix LaTeX compile errors and check how pages, tables and figures render.
- Add verified citations and find broken ones.
- Prepare an arXiv bundle and a tracked-changes PDF for a revision.
- Needed before connecting: a repository link and an access token. Overleaf's Git token needs a paid Overleaf
  plan; GitHub, GitLab and Bitbucket tokens are free.
- Data: reads and writes.

## Step: Company

- Company name: MiLatexAI
- Website: https://milatexai.com
- Primary contact: your email

## Step: Authentication

OAuth with dynamic client registration (WorkOS AuthKit).

## Step: Data handling

- The API is our own (first party).
- No personal health data. No sponsored content.

## Step: Test and launch (reviewers read this text, so the payment explanation goes here)

Paste, filling in the two blanks:

> Add the connector in Claude (Settings, Connectors, Add custom connector, URL https://milatexai.com/mcp) and sign
> in with email ____ and password ____. This account is on Pro and already has a small sample paper connected
> (a private GitHub repository named "milatexai-review-paper"), so every tool works without any setup.
>
> Things to try: "list my projects", "show the outline of main.tex", "fix the typo in the abstract" (edit_file),
> "undo that change" (restore_file), "does the paper compile?" (check_compile), "show me table 1" (show_table),
> "show page 1" (show_page), "add the citation arXiv 1706.03762" (add_citation), "check my citations",
> "save a checkpoint", "make a bar chart of table 1 and save it" (commit_figure).
>
> About payments: `upgrade` and `manage_subscription` do not execute financial transactions. They only return a
> link to a page hosted by Stripe (a checkout page or the customer billing portal). The connector never charges
> anyone, never sees or stores card details, and never moves money; the person decides and pays, or cancels, on
> Stripe's own page. On this reviewer account, `upgrade` replies that the account is already Pro and
> `manage_subscription` replies that there is no paid subscription to manage, so neither creates a Stripe page.
>
> Each write is a real Git commit on the repository, so every change is visible in its history and reversible.

Confirm you ran every tool (I can run them all through the MCP client against the reviewer account and give you a
pass list before you tick this).

## Step: Compliance (seven acknowledgements)

Directory guidelines, first-party API, financial transactions, AI media generation, prompt injection, conversation
data collection, public documentation. Notes for two of them:

- **Financial transactions**: the connector executes none (see the reviewer note above). The policy's ban is on
  software that "transfers money ... or executes financial transactions on behalf of users".
- **AI media generation**: no AI images, video or audio. `commit_figure` runs the user's own plotting code, and the
  rules explicitly allow diagrams and charts.

## If a reviewer still objects to the payment links

Remove `upgrade` and `manage_subscription` from the server and resubmit. Upgrading still works: the message shown
when someone reaches the free limit already contains the upgrade link, and billing can be managed from
https://milatexai.com/account.
