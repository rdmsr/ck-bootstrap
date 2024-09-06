from cutekit import shell, vt100, cli, model, const, jexpr
import os
import pathlib
import hashlib
import tarfile
import tempfile
import dataclasses as dt
import subprocess

containerNamePrefix = "CK__"
defaultImage = "debian"
defaultContainerName = f"{containerNamePrefix}default"
defaultMachineName = f"{containerNamePrefix}machine"
cacheSourcesDir = os.path.join(const.CACHE_DIR, "sources")
cacheBuildsDir = os.path.join(const.CACHE_DIR, "builds")

@dt.dataclass
class Image:
    id: str
    setup: list[str]

IMAGES: dict[str, Image] = {
    "debian": Image(
        "debian",
        [
            "apt-get update",
            "apt-get install -y python3 python3-pip python3-venv ninja-build build-essential git",
        ],
        )
}

class InitArgs(model.RegistryArgs):
    image: str = cli.arg(None, "image", "Which OS image to use", default=defaultImage)



def printProgress(str):
    print(f"{vt100.BOLD}{str}... {vt100.RESET}", end="", flush=True)

def printDone():
    print(f"{vt100.GREEN}Done{vt100.RESET}")


def machineExists(name: str) -> bool:
    try:
        shell.exec("podman", "machine", "inspect", name, quiet=True)
        return True
    except:
        return False

def containerExists(name: str) -> bool:
    try:
        shell.exec("podman", "container", "exists", name)
        return True
    except:
        return False
    

def createContainer(name: str, image: str):
    shell.exec("podman", "run", "-v", f"{os.getcwd()}:/cutekit-bootstrap", "-dit", "--name", name, image, "/bin/bash")
    img = IMAGES[image]

    for cmd in img.setup:
        execInContainer(name, cmd)

def execInContainer(name: str, command: str):
    try:
        shell.exec("podman", "exec", name, "/bin/sh", "-c", f"{command}")
    except shell.ShellException:
        shell.exec("podman", "restart", name)
        shell.exec("podman", "exec", name, "/bin/sh", "-c", f"{command}")
        

def runCutekitCommandInContainer(container: str, command: str):
    execInContainer(container, f"cd /cutekit-bootstrap && ./meta/plugins/run.sh bootstrap {command} --in-container=true")
     
def tryCreateContainer(name: str = defaultContainerName, image: str = defaultImage) -> None:
    if not containerExists(name):
        createContainer(name, image)


def tryCreateMachine() -> None:
   printProgress(f"Starting machine '{defaultMachineName}'")
   if not machineExists(defaultMachineName):
       shell.exec("podman", "machine", "stop")
       shell.exec("podman", "machine", "init", defaultMachineName, "--rootful", "-v", os.getcwd(), "--now", quiet=True)
   else:
       try:
           shell.exec("podman", "machine", "start", defaultMachineName, quiet=True)
       except shell.ShellException:
           pass
   # This sucks, but it seems like we have no other choice
   shell.exec("podman", "system", "connection", "default", defaultMachineName)

   printDone()



def fetchRecipe(expr: dict) -> None:
    sources_dir = os.path.join(cacheSourcesDir, expr["id"])

    if os.path.exists(sources_dir):
        return

    url = expr["source"]["url"]
    method = expr["source"]["method"]
    has_checksum = expr["source"].get("checksum") is not None
    name = expr["id"]

    if not has_checksum:
        vt100.warning(f"'{name}' has no source checksum specified... data integrity will not be verified")


    printProgress(f"Fetching recipe '{name}'")

    # TODO: Add git support
    if method != "tarball":
        raise RuntimeError(f"Unknown source method '{method}'")

    path = shell.wget(url)
    printDone()

    with open(path, 'rb') as f:
        if has_checksum:
            checksum_parts = expr["source"]["checksum"].split(':')
            
            if checksum_parts[1] != str(hashlib.file_digest(f, checksum_parts[0]).hexdigest()):
                raise RuntimeError("Could not verify data integrity: invalid checksum")

    if not os.path.exists(cacheSourcesDir):
        shell.mkdir(cacheSourcesDir)

    # Make a temporary directory and extract there
    tmpdir = tempfile.mkdtemp(dir=const.CACHE_DIR)

    tf = tarfile.open(path)

    tf.extractall(tmpdir)

    # Move all files in sources/<recipe>, this is to avoid having something like sources/hello/hello-2.1
    for file in os.listdir(tmpdir):
        shell.mv(os.path.join(tmpdir, file), sources_dir)

    tf.close()
    os.rmdir(tmpdir)

    shell.cpTree(sources_dir, sources_dir + '-clean')


def buildRecipe(expr: dict, quiet: bool) -> None:
    build_dir = os.path.join(cacheBuildsDir, expr['id'])
    sources_dir = os.path.join(cacheSourcesDir, expr['id'])

    built_file = os.path.join(cacheBuildsDir, expr['id'] + '.built')

    if wasRecipeBuilt(expr['id']):
        return
    
    steps = expr["steps"]

    printProgress(f"Building recipe '{expr['id']}'")

    shell.cpTree(sources_dir, build_dir)

    for step in steps["build"]:
        shell.exec(*step.split(' '), cwd=build_dir, quiet=quiet)

    printDone()

    with open(built_file, 'w') as f:
        f.write('')
        

def packageRecipe(expr: dict) -> None:
    steps = expr["steps"]
    printProgress(f"Packaging recipe '{expr['id']}'")

    build_dir = os.path.join(cacheBuildsDir, expr['id'])
    sources_dir = os.path.join(cacheSourcesDir, expr['id'])

    shell.cpTree(sources_dir, build_dir)

    for step in steps["package"]:
        shell.exec(*step.split(' '), cwd=build_dir)

    printDone()


def setupContainer(image: str):
    if shell.uname().machine != "linux":
        tryCreateMachine()

    tryCreateContainer(image=image)


def doBuild(recipe: str, quiet: bool):
    if os.path.exists(f"recipes/{recipe}.json"):
        expr = jexpr.read(pathlib.Path(f"recipes/{recipe}.json"))
        fetchRecipe(expr)
        buildRecipe(expr, quiet)
    else:
        raise RuntimeError(f"No such recipe: {recipe}")


def wasRecipeBuilt(recipe: str):
    built_file = os.path.join(cacheBuildsDir, recipe + '.built')
    return os.path.exists(built_file)

    
@cli.command(None, "bootstrap", "Bootstrap distribution")
def _():
    pass


class BuildArgs(model.RegistryArgs):
    name: str = cli.arg(None, "recipe", "Recipe to build")
    in_container: bool = cli.arg(False, "in-container", "Whether or not this is run in the container")
    quiet: bool = cli.arg(False, "quiet", "Whether or not to silence command output")



class BuildAllArgs(model.RegistryArgs):
    in_container: bool = cli.arg(False, "in-container", "Whether or not this is run in the container")
    quiet: bool = cli.arg(False, "quiet", "Whether or not to silence command output")


class PatchArgs(model.RegistryArgs):
    recipe: str = cli.arg(None, "recipe", "Recipe to patch")

@cli.command(None, "bootstrap/build-all", "Build all packages")
def _(args: BuildAllArgs):

    if args.in_container:
        if os.path.exists("recipes"):
            for file in os.listdir("recipes"):
                recipe_name = file.split('.')[0]

                if wasRecipeBuilt(recipe_name):
                    print(f"{recipe_name}: no work to do")
                    continue

                doBuild(recipe_name, args.quiet)

        else:
            raise RuntimeError("No 'recipes' directory!")
    else:
        runCutekitCommandInContainer(defaultContainerName, f"build-all --quiet={args.quiet}")
            

@cli.command(None, "bootstrap/build", "Build a recipe")
def _(args: BuildArgs):
    if wasRecipeBuilt(args.name):
        print("No work to do")
        return

    if args.in_container:
        doBuild(args.name, args.quiet)
    else:
        runCutekitCommandInContainer(defaultContainerName, f"build --recipe={args.name} --quiet={args.quiet}")

@cli.command(None, "bootstrap/rebuild", "Rebuild a recipe")
def _(args: BuildArgs):
    built_file = os.path.join(cacheBuildsDir, args.name + '.built')

    if wasRecipeBuilt(args.name):
        shell.rmrf(built_file)

    if args.in_container:
        if os.path.exists(f"recipes/{args.name}.json"):
            expr = jexpr.read(pathlib.Path(f"recipes/{args.name}.json"))
            fetchRecipe(expr)
            buildRecipe(expr, args.quiet)
        else:
            raise RuntimeError(f"No such recipe: {args.name}")
    else:
        runCutekitCommandInContainer(defaultContainerName, f"build --recipe={args.name} --quiet={args.quiet}")


@cli.command(None, "bootstrap/make-patch", "Start the patching process")
def _(args: PatchArgs):
    sources_clean_dir = os.path.join(cacheSourcesDir, args.recipe + "-clean")

    # TODO: fetch if not fetched already
    if not os.path.exists(sources_clean_dir):
        raise RuntimeError("Recipe sources were not fetched yet")

    shell.cpTree(sources_clean_dir, f"{args.recipe}-workdir")
    print(f"Created new directory '{args.recipe}-workdir', make your changes and run 'save-patch'")


@cli.command(None, "bootstrap/save-patch", "Save modifications into a patch")
def _(args: PatchArgs):
    workdir = (args.recipe + "-workdir")
    sources_clean_dir = os.path.join(cacheSourcesDir, args.recipe + "-clean")

    proc = subprocess.run(["git", "diff", "--no-index", "--no-prefix", sources_clean_dir, workdir], check=False, stdout=subprocess.PIPE)

    with open(args.recipe + ".patch", "wb") as f:
        f.write(proc.stdout)
        

    print(f"Saved patch to {args.recipe+'.patch'}")

    remove = vt100.ask("Remove workdir directory?", default=True)

    if remove:
        shell.rmrf(workdir)





# TODO: Do container setup in 'build' if init wasn't called
@cli.command("i", "bootstrap/init", "Init container")
def _(args: InitArgs):
    if args.image not in IMAGES:
        raise RuntimeError(f"Invalid image, available options are {IMAGES}")

    setupContainer(IMAGES[args.image].id)
