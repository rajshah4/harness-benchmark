# Virtual Spread Plate Lab

Build a polished, self-contained web application that demonstrates the spread plate technique for a biology class. This is a greenfield artifact task adapted from ArtifactsBench task 231.

The lab must show these materials and pieces of equipment:

- a test tube with yeast culture broth
- a cotton plug
- solid agar culture medium in a plate
- an inoculation loop
- an alcohol lamp with a clear flammable-material warning
- a clean bench or sterile work area

The objects should look recognizable, use colors that resemble the real objects, appear on both sides of the work area, and be movable. Use original CSS, HTML, SVG, or canvas artwork. Do not use remote assets, external libraries, CDNs, or network requests. The finished app must run by opening or statically serving `index.html`.

## Learning flow

Guide the learner through this scientifically valid sequence:

1. Sterilize the inoculation loop with the alcohol lamp.
2. Cool the loop in the sterile area.
3. Sample the yeast culture.
4. Inoculate the agar plate.
5. Spread the sample across the plate.
6. Incubate and show a completed result.

Prevent impossible or unsafe actions. An invalid action must leave the current step unchanged and provide friendly, visible feedback that explains what to do next. The learner must be able to reset the experiment and start again. The page should tolerate repeated clicks and remain usable on a phone-sized viewport.

Make the experience interesting enough to demonstrate in a workshop. Add at least one useful educational enhancement, such as time-lapse growth, a microscope view, a contamination indicator, a short quiz, or an explanation of why each step matters.

## Behavior contract

The instructor-owned checker uses this small public contract so the visual design remains open-ended.

Expose this object after the page loads:

```javascript
window.spreadPlateLab = {
  getState(),
  perform(action),
  reset()
}
```

`getState()` must return an object with:

- `step`: one of `sterilize`, `cool`, `sample`, `inoculate`, `spread`, `incubate`, or `complete`
- `completed`: a boolean
- `history`: an array of successfully completed action names

`perform(action)` must accept `sterilize`, `cool`, `sample`, `inoculate`, `spread`, and `incubate`. It must return an object containing an `ok` boolean and a human-readable `message`. It may update the page synchronously or return a Promise. Calling an action out of order must return `ok: false` without advancing the step.

`reset()` must restore the initial state.

Add these stable DOM markers:

- `data-testid="lab-root"` on the main application, with its current step in `data-step`
- `data-testid="equipment-yeast-broth"`
- `data-testid="equipment-cotton-plug"`
- `data-testid="equipment-agar-plate"`
- `data-testid="equipment-inoculation-loop"`
- `data-testid="equipment-alcohol-lamp"`
- `data-testid="equipment-clean-bench"`
- `data-testid="step-instruction"` for the current instruction
- `data-testid="feedback"` for action feedback
- `data-testid="progress"` for visible progress
- one visible interactive element with `data-action` for each procedure action
- one visible interactive element with `data-action="reset"`

Every equipment marker must be draggable through native drag behavior or pointer interactions. Set `draggable="true"` on each marked equipment element even if you also support pointer or touch dragging.

## Completion expectations

Build the complete experience, not a wireframe. Keep the code organized and readable. Include a short `README.md` explaining how to run the page and summarizing the interaction design. Before finishing, serve the app locally, exercise the full sequence and at least one invalid action, inspect the phone layout, and check the browser console for errors.
