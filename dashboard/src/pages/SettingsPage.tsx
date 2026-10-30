/**
 * OBS-08: the connection form backing `lib/apiConfig.tsx` -- a real
 * multi-tenant deployment needs a per-browser credential, not a single
 * build-time constant (see that module's own docstring).
 */

import { useState } from "react";

import { useApiConfig } from "../lib/apiConfig";

export function SettingsPage() {
  const { baseUrl, apiKey, setBaseUrl, setApiKey } = useApiConfig();
  const [draftBaseUrl, setDraftBaseUrl] = useState(baseUrl);
  const [draftApiKey, setDraftApiKey] = useState(apiKey);
  const [saved, setSaved] = useState(false);

  return (
    <section aria-labelledby="settings-heading">
      <h2 id="settings-heading">Connection settings</h2>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setBaseUrl(draftBaseUrl);
          setApiKey(draftApiKey);
          setSaved(true);
        }}
      >
        <div>
          <label htmlFor="base-url">
            Query API base URL
            <input
              id="base-url"
              type="text"
              value={draftBaseUrl}
              onChange={(event) => {
                setDraftBaseUrl(event.target.value);
                setSaved(false);
              }}
              placeholder="(leave blank to use this page's own origin)"
            />
          </label>
        </div>
        <div>
          <label htmlFor="api-key">
            API key
            <input
              id="api-key"
              type="password"
              value={draftApiKey}
              onChange={(event) => {
                setDraftApiKey(event.target.value);
                setSaved(false);
              }}
              autoComplete="off"
            />
          </label>
        </div>
        <button type="submit">Save</button>
        {saved && <span role="status"> Saved.</span>}
      </form>
      <p>
        The API key is stored only in this browser (<code>localStorage</code>) and sent only to the
        configured Query API base URL, in the <code>Authorization</code> header of each request.
      </p>
    </section>
  );
}
