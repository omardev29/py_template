# Assets

Everything you put here is packaged with the game (exe, portable and pyz).
Load a file with `{{pkg}}.resources.asset("name.png")`, or with `gfx.load_texture("name.png")`
for textures. Paths work the same in development and in the executable.

Give raylib a file's bytes, not its path (`gfx.load_texture` reads the file and calls
`LoadImageFromMemory`; sounds have `LoadWaveFromMemory`, fonts `LoadFontFromMemory`): raylib
opens a path with the C `fopen`, which on Windows misreads a folder name with an accent.
