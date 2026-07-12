Refactor Config Loader to eliminate the unnecessary CLI code

Is Diagnostics.Py actually used anywhere? If not remove it

Is direct streamer still used

Change stt configuration to eliminate verbose logging messages

## PRIORITY 1: Hardware Layer EventBus Refactoring (Modified Big Bang Approach)

**Context**: After 0.4.0 release, identified that hardware layer components (MouseHandler, HIDListener, etc.) have tight coupling that makes new features and bug fixes difficult. Speech pipeline is too complex/risky to refactor, so focusing on hardware layer first.

**Strategy**: Modified Big Bang - refactor all hardware/integration components together to maintain architectural coherence, then tackle speech pipeline later.

**Session Plan**:
- Session 1: Architecture Planning + HIDListener foundation (design event contracts for all components)
- Session 2: SoftwareDimmer → EventBus + ConfigService  
- Session 3: BraviaControl + SonosControl → EventBus + ConfigService
- Session 4: MouseHandler → EventBus integration (remove direct service coupling)

**Target Event Architecture**:
```
HIDListener → ThumbWheelEvent → EventBus
MouseHandler → VolumeChangeRequest → EventBus → SonosControl/BraviaControl  
MouseHandler → DimmingRequest → EventBus → SoftwareDimmer
```

**Benefits**: Breaks MouseHandler tight coupling, standardizes hardware input patterns, enables easier hardware feature additions, avoids scary speech pipeline complexity.

**Components to Refactor**:
- MouseHandler (❌ tight coupling to 3+ services)
- HIDListener (❌ uses asyncio.Queue instead of EventBus)
- BraviaControl (❌ no EventBus/ConfigService)
- SonosControl (❌ no EventBus/ConfigService) 
- SoftwareDimmer (❌ no EventBus/ConfigService)

The whole pipeline starting at speech Handler is very difficult to follow. Maybe we can make it clearer. **[DEFERRED: Speech pipeline refactoring after hardware layer is complete]**
Change the actions and translations Jason's file to toml so that we can include comments in those files to explain how to modify them comments in those files to explain how to modify them and how they work in line with the code AS documentation principles of this project. From the developer point of view, document the currently available actions and how to introduce new actions. We should Trace every existing action specified in the Json file make sure it's implemented. I know that "activate" is not implemented correctly.

Create new translation that converts ().*$ to (.*)

Create new action that converts ^do it$ to do it enter

Introduce a new command, something like note to self, which presents a simple user interface such as the Terminal editor or notepad and allows notes to be added and Saved. Need to think about this to refine the requirement. 

Create new translation that converts ().*$ to (.*)

Create a new translation or action , not sure which, to convert a spelled out word into the word. So "h e l l o" for example becomes "hello"

The space bar translation is not working. The single space may be stripped before it reaches the focused control

Get an explanation for async def _demux_loop in Wheelhouse App. Consider changing app to a better name more descriptive of its actual function.

Change all Jason configurations to T o M l to allow documentation comments

Discuss refactoring entire project to do dependency injection for class Constructors instead of having each class do its own access to the config data. Audio Monitor is a prime candidate for example. A big part of this refactoring would be to eliminate searches for location of files for example, or making assumptions about what are parameters should be without what are parameters should be without looking at the actual configuration.

config.py is very confusing. Document and or rewrite it

Test and debug window mover

Refactor HIDListener to Listen to Only the appropriate configured Mouse not to everything the mouse sends. Maybe.

mouse_handler.py: software dimmer is not used. figure out what to do with that code. Do a bit of Cleanup in the process.

sonos_monitor.py: there is no need to monitor every Sonos speaker on the network. Probably only monitor living room speaker but I'm not sure

input_proc.py: why is argparse.ArgumentParser() necessary in this module? Also need much better understanding of logic especially the clipboard handling aspect. Possible refactoring?

bravia_control.py: need to control at least one other kind of standard brightness capabilities , that of a laptop. New integration for that needed.

gemini_client.py: need to integrate this with a command like fix this or something similar. The idea is that when user says fix this inside a text control , the entire contents of the text control are to Gemini for textual correction, then show the results to the user for editing and or replacing what's already in the text control

Need more clarity about how the speech to text server and the websocket manager, work together and where they fit in the command and dictation routing command and dictation routing flow

Wheelhouse has a menu item that is supposed to restart the Wheelhouse application. It does not work anymore. Need to fix it. Might be a problem with launcher.

AI agents seem to be confused from time to time regarding the path of imported modules, particularly relative and absolute paths. I need to analyze the handling of the system path and reconcile it across the project.

Should launcher be part of a flow?

We need an explicit policy and software to handle that policy regarding installation on systems that do not incorporate Sonos or Bravia. Probably fall back to system sound and brightness controls. Even though brightness controls don't typically work for desktop systems as far as I know. We should discuss whether features like this should be implemented somehow as plugins. However this project is such a spider web connections it may be difficult to do something like that.

command_engine.py: can it be made clearer , less complicated

translator.py: I think we can eliminate this module altogether. I put this in place originally to do what Google speech to text is now doing for me.
