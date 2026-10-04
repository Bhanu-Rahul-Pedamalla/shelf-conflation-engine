# Recording a clip of the opening view

The page has a clip mode that shows the opening alone, sized for a phone: the narrator's line, the shelf, the worker,
then the viewfinder with its counters, the strip and one caption line. Nothing else is drawn. The opening starts by
itself half a second after the page loads, so the first frame is clean: the line "Look at this shelf." over an empty stage,
then the shelf draws itself.

| Flag | Frame | Use |
| --- | --- | --- |
| `?clip=1` | 9:16, record at 1080 x 1920 | Instagram Reels, TikTok, X vertical |
| `?clip=square` | 1:1, record at 1080 x 1080 | X, LinkedIn |

In clip mode each read is held 1.1 s instead of 0.8 s, so a viewer on a phone catches the colouring. The caption says so.
Every read, verdict and number is the same as on the page: nothing is staged for the clip.

## Steps on Windows 11, nothing to install

1. Open the page in a window of exactly the frame size. Chrome and Edge both take these flags; the `--app` flag drops
   the address bar and tabs so only the page is on screen.

   ```
   start chrome --app="https://shelf-replay.web.app/web/?clip=1" --window-size=1080,1920
   start chrome --app="https://shelf-replay.web.app/web/?clip=square" --window-size=1080,1080
   ```

   For a local copy, serve the repository root first (`python -m http.server`) and use `http://localhost:8000/web/?clip=1`.
   If the window cannot be taller than the screen, record a 1080 x 1080 clip, or set the display scale to 100 % and
   use a monitor at least 1920 px tall. The 9:16 frame needs that height.

2. Start the recording before the walk starts. Either built-in recorder works:
   - Snipping Tool: `Win + Shift + S`, choose the video camera, draw the region over the window, press Start. Stop from
     the toolbar; the file is saved under Videos \ Screen Recordings.
   - Xbox Game Bar: click the page window, press `Win + Alt + R` to start and again to stop. The file is saved under
     Videos \ Captures. Game Bar records the active window at its own size, so the window must already be the frame size.

3. Reload the page (`F5`) the moment the recording starts, so the clip opens on the clean first frame. The shelf is drawn
   inside the first second, the worker enters at 4 s, the viewfinder opens at 7 s and the reads start at once. A 30 s clip
   shows the shelf, the worker and about twenty reads with their verdicts.

4. Trim in the Photos app if needed (open the file, Edit, Trim). No re-encoding settings to choose.

## Checking the result

- The first frame shows the line "Look at this shelf." and nothing else; the shelf then draws in from the left.
- Every verdict pill colour in the clip must match the strip mark under the badge; both come from `data/baseline_results.csv`.
- The counters at the end of the aisle read 20 matched, 38 no answer, 0 wrong of 58 for store S001, aisle A05, day 1.
