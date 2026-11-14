# API files to import

Import these files into your API client and every FaceMatch endpoint is ready to send, with its parameters,
example values, file fields and the API key filled in.

| File | Format | Use it in |
|---|---|---|
| [FaceMatch.postman_collection.json](FaceMatch.postman_collection.json) | Postman Collection v2.1 | Postman, Insomnia, Bruno, Hoppscotch, Thunder Client, Apidog |
| [FaceMatch.postman_environment.json](FaceMatch.postman_environment.json) | Postman environment | Postman (holds `baseUrl` and your `apiKey`) |
| [openapi.json](openapi.json) | OpenAPI 3.1 | Swagger UI/Editor, Insomnia, Bruno, Hoppscotch, Apidog, Redocly, code generators |

All three are generated from the code (`face-match export-api`), so they always match the server. A running
server also serves its own spec at `GET /openapi.json`. That's the right one to use if your server runs a
different version.

## Import by link (no download)

```
https://raw.githubusercontent.com/prakashsolanki28/face-match-service/main/api/FaceMatch.postman_collection.json
https://raw.githubusercontent.com/prakashsolanki28/face-match-service/main/api/FaceMatch.postman_environment.json
https://raw.githubusercontent.com/prakashsolanki28/face-match-service/main/api/openapi.json
```

These links describe the latest code. For a specific release, replace `main` with its tag (e.g. `v2.0.0`). The
files are also attached to every [GitHub release](https://github.com/prakashsolanki28/face-match-service/releases).

## Postman

1. **Import** (top left). Drop in both `FaceMatch.postman_collection.json` and
   `FaceMatch.postman_environment.json`, or paste the two links above.
2. Choose the **FaceMatch** environment (top right) and edit it:
   - `baseUrl`: your server, e.g. `http://localhost:8000` or `https://face.example.com`
   - `apiKey`: your API key (the `API_KEY` setting; see [docs/api-keys.md](../docs/api-keys.md)). It's a
     **secret** variable, so it's masked on screen.
3. Send **System → Health check**, then **Auth → Check API key** to check that your key works.
4. For photos, open **Body → form-data** and choose a file in each `image` / `images` row. To enroll with several
   photos, add more `images` rows.

The collection sends the key in the `X-API-Key` header for every request, except the public ones (health check,
service info). The one key gives access to everything. **Enroll a person** saves the new id in the `personId`
variable, so **Get a person**, **Add photos** and **Delete a person** work straight away. Deletes are the last
request in each folder, so you can run a whole folder (or the collection) from top to bottom.

For a test and a live server, duplicate the environment and change `baseUrl` and `apiKey` in the copy.

> Import the **collection** into Postman rather than `openapi.json`. Postman's OpenAPI importer turns the
> multi-photo `images` field into a text field and switches on every optional filter.

## Other tools

| Tool | How |
|---|---|
| **Insomnia** | **Import** → file or URL → the Postman collection or `openapi.json`. Set the `baseUrl` and `apiKey` environment variables |
| **Bruno** | **Import Collection** → **Postman Collection** (or **OpenAPI V3 Spec**) |
| **Hoppscotch** | **Collections** → **Import** → **Postman** (or **OpenAPI**) |
| **Thunder Client** (VS Code) | **Collections** → menu → **Import** → the Postman collection |
| **Apidog** | **Import** → **Postman** or **OpenAPI/Swagger** |
| **Swagger Editor** | [editor.swagger.io](https://editor.swagger.io) → **File** → **Import file** → `openapi.json` |
| **Built-in docs** | Start the server with `API_DOCS=true` and open `/docs` (Swagger UI) or `/redoc` |

When a tool sends requests from a web page (Swagger Editor, Hoppscotch on the web), the browser applies CORS. Add
the tool's address to `CORS_ORIGINS` on the server, or use the desktop version of the tool.

## Generate a client

`openapi.json` follows OpenAPI 3.1. Operation ids are readable (`enroll_person`, `recognize_faces`,
`update_person` …), and every operation that needs the API key declares it (`X-API-Key` or Bearer).

```bash
# TypeScript types
npx openapi-typescript api/openapi.json -o facematch.d.ts

# Python client
pipx run openapi-python-client generate --path api/openapi.json
```

Other generators work too, as long as they support OpenAPI 3.1.

## Keeping the files current

After changing the API, regenerate the files and commit them:

```bash
face-match export-api            # writes api/
face-match export-api --check    # exits 1 if api/ is out of date
```

The test suite fails if the committed files no longer match the code.
