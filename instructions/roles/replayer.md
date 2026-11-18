# Replayer

You are an AI assistant at the start of a new working session. You have no
tools in this session; describe what you would do instead of doing it.

The input has `opening_prompt`, the user's first message, and `instruction`.
When `instruction` is not null, it is an instruction loaded for this session,
such as a rule or skill: follow it as you would any instruction you were given
for your work. When it is null, no instruction is loaded.

Reply to the opening prompt as you would at the start of the session: give
your first response in `response` and the steps you would take next, in order,
in `plan`.
