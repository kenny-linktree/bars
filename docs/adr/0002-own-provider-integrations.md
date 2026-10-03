# Own the provider integrations

Bars will fetch provider data directly through its own adapters, with no CodexBar runtime dependency. An independent implementation is an explicit requirement, accepting the need for Bars to maintain authentication and response handling that the previous widget delegated to CodexBar. Existing endpoint findings can inform the implementation, but the old CLI-based collection path will not be retained.
