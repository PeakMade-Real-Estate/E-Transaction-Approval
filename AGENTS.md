# GitHub Copilot Repository Instructions

This repository is used to build and maintain Python Flask applications.
Source control is managed in GitHub.
Applications are deployed to Azure.

## Core behavior rules

- Do not hallucinate facts, files, configurations, routes, environment variables, secrets, package versions, Azure resources, or deployment settings.
- If required context is missing, say clearly that the answer is uncertain and ask for the missing context.
- Before proposing a fix, check whether the issue was caused by hardcoded values, assumptions, or fabricated project details.
- Do not present stream-of-consciousness reasoning or internal thought processes.
- Think through the problem internally, then present the answer in a clear, ordered, and cohesive format.
- Prefer direct, practical answers over speculative explanations.

## Repository awareness rules

- Respect the existing repository structure, coding patterns, naming conventions, dependency choices, and project conventions before introducing new ones.
- Do not invent files or claim a file exists unless it is present in the repository context.
- Do not assume the presence of Azure resources, GitHub Actions workflows, deployment slots, environment variables, or infrastructure unless they are shown in the repository or provided in context.
- If the implementation depends on missing repository context, state exactly what file, configuration, or example is needed.

## Hardcoding and configuration rules

- Check for hardcoded values before proposing other causes of failure.
- Do not hardcode secrets, tokens, connection strings, subscription IDs, tenant IDs, URLs, ports, database names, app settings, or resource names.
- Prefer environment variables, configuration files, and existing application settings patterns for deployment-specific values.
- If hardcoding appears to be the problem, point it out explicitly and recommend the existing project-approved configuration pattern.

## Flask rules

- Use idiomatic Flask patterns that match the current repository.
- Prefer simple, maintainable solutions over unnecessary abstractions.
- Keep configuration, routes, services, and integrations separated when the project already follows that pattern.
- Validate request input and handle errors explicitly.
- For new endpoints, include validation, error handling, and authentication or authorization checks when the project requires them.
- Do not introduce major new frameworks or dependencies unless there is a clear repository-supported reason.

## GitHub change rules

- When proposing code changes, be explicit about which files should change.
- Prefer complete, working edits over disconnected snippets when possible.
- Avoid broad refactors unless they are requested.
- Preserve backward compatibility unless the task explicitly requires a breaking change.
- Do not claim code was run, tested, reviewed, or validated unless that evidence is present in the context.

## Azure deployment rules

- Do not invent Azure configuration values.
- Do not fabricate app settings, service names, deployment targets, pipeline values, resource groups, or infrastructure details.
- If Azure behavior depends on unavailable configuration, identify the exact missing setting, file, or deployment artifact.
- Prefer solutions that keep local development configuration and Azure deployment configuration clearly separated.

## Response quality rules

- When uncertain, clearly state:
  - what is known,
  - what is missing,
  - what assumption would be required,
  - and what context is needed next.
- When diagnosing bugs, check hardcoded values, environment-specific logic, and hidden assumptions first.
- Present findings in an ordered and cohesive manner.
- Do not pretend certainty when repository context is incomplete.