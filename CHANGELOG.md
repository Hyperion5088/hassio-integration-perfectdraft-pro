# Changelog

## 0.6.4

- Fix Home Assistant reauthentication so successful PerfectDraft sign-in updates
  and reloads the existing config entry instead of reporting that the account is
  already configured.
- Preserve existing options and unrelated config-entry data while replacing the
  account tokens and machine ID.
- Reject reauthentication with a different PerfectDraft account.
