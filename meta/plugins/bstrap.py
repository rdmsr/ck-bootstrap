from dataclasses_json import DataClassJsonMixin
from cutekit import shell, vt100, cli, model, const, jexpr
from typing import Optional
from pathlib import Path

import hashlib
import tarfile
import tempfile
import dataclasses as dt
import subprocess

containerNamePrefix = "CK__"
defaultImage = "debian"
defaultContainerName = f"{containerNamePrefix}default"
defaultMachineName = f"{containerNamePrefix}machine"
cacheSourcesDir = Path(const.CACHE_DIR) / "sources"
cacheBuildsDir = Path(const.CACHE_DIR) / "builds"
recipeDir = Path("recipes")


@dt.dataclass
class Steps(DataClassJsonMixin):
    build: list[str]
    package: list[str]


@dt.dataclass
class RecipeRequirements(DataClassJsonMixin):
    host: list[str]


@dt.dataclass
class RecipeSource(DataClassJsonMixin):
    url: str
    method: str
    checksum: Optional[str]


@dt.dataclass
class Recipe(DataClassJsonMixin):
    id: str
    source: RecipeSource
    steps: Steps


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


def printProgress(str) -> None:
    print(f"{vt100.BOLD}{str}... {vt100.RESET}", end="", flush=True)


def printDone() -> None:
    print(f"{vt100.GREEN}Done{vt100.RESET}")


def machineExists(name: str) -> bool:
    try:
        shell.exec("podman", "machine", "inspect", name, quiet=True)
        return True
    except shell.ShellException:
        return False


def containerExists(name: str) -> bool:
    try:
        shell.exec("podman", "container", "exists", name)
        return True
    except shell.ShellException:
        return False


def createContainer(name: str, image: str) -> None:
    shell.exec(
        "podman",
        "run",
        "-v",
        f"{Path.cwd()}:/cutekit-bootstrap",
        "-dit",
        "--name",
        name,
        image,
        "/bin/bash",
    )
    img = IMAGES[image]

    for cmd in img.setup:
        execInContainer(name, cmd)


def execInContainer(name: str, command: str) -> None:
    try:
        shell.exec("podman", "exec", name, "/bin/sh", "-c", f"{command}")
    except shell.ShellException:
        shell.exec("podman", "restart", name)
        shell.exec("podman", "exec", name, "/bin/sh", "-c", f"{command}")


def runCutekitCommandInContainer(container: str, command: str) -> None:
    execInContainer(
        container,
        f"cd /cutekit-bootstrap && ./meta/plugins/run.sh bootstrap {command} --in-container=true",
    )


def tryCreateContainer(
    name: str = defaultContainerName, image: str = defaultImage
) -> None:
    if not containerExists(name):
        createContainer(name, image)


def tryCreateMachine() -> None:
    printProgress(f"Starting machine '{defaultMachineName}'")
    if not machineExists(defaultMachineName):
        shell.exec("podman", "machine", "stop")
        shell.exec(
            "podman",
            "machine",
            "init",
            defaultMachineName,
            "--rootful",
            "-v",
            str(Path.cwd()),
            "--now",
            quiet=True,
        )
    else:
        try:
            shell.exec("podman", "machine", "start", defaultMachineName, quiet=True)
        except shell.ShellException:
            pass
    # This sucks, but it seems like we have no other choice
    shell.exec("podman", "system", "connection", "default", defaultMachineName)

    printDone()


def fetchRecipe(r: Recipe) -> None:
    sources_dir = cacheSourcesDir / r.id

    if sources_dir.exists():
        return

    if r.source.checksum is None:
        vt100.warning(
            f"'{r.id}' has no source checksum specified... data integrity will not be verified"
        )

    printProgress(f"Fetching recipe '{r.id}'")

    # TODO: Add git support
    if r.source.method != "tarball":
        raise RuntimeError(f"Unknown source method '{r.source.method}'")

    path = shell.wget(r.source.url)
    printDone()

    with open(path, "rb") as f:
        if r.source.checksum is not None:
            checksum_parts = r.source.checksum.split(":")

            if checksum_parts[1] != str(
                hashlib.file_digest(f, checksum_parts[0]).hexdigest()
            ):
                raise RuntimeError("Could not verify data integrity: invalid checksum")

    cacheSourcesDir.mkdir(parents=True, exist_ok=True)

    # Make a temporary directory and extract there
    tmpdir = tempfile.mkdtemp(dir=const.CACHE_DIR)

    tf = tarfile.open(path)

    tf.extractall(tmpdir)

    # Move all files in sources/<recipe>, this is to avoid having something like sources/hello/hello-2.1
    for file in Path(tmpdir).iterdir():
        shell.mv(str(file), str(sources_dir))

    tf.close()
    shell.rmrf(str(tmpdir))

    shell.cpTree(str(sources_dir), f"{sources_dir}-clean")


def buildRecipe(r: Recipe, quiet: bool) -> None:
    build_dir = cacheBuildsDir / r.id
    sources_dir = cacheSourcesDir / r.id
    built_file = cacheBuildsDir / f"{r.id}.built"

    if wasRecipeBuilt(r.id):
        return

    printProgress(f"Building recipe '{r.id}'")

    shell.cpTree(str(sources_dir), str(build_dir))

    for step in r.steps.build:
        shell.exec(*step.split(" "), cwd=str(build_dir), quiet=quiet)

    printDone()

    with built_file.open("w") as f:
        f.write("")


def packageRecipe(r: Recipe) -> None:
    printProgress(f"Packaging recipe '{r.id}'")

    build_dir = cacheBuildsDir / r.id
    sources_dir = cacheSourcesDir / r.id

    shell.cpTree(str(sources_dir), str(build_dir))

    for step in r.steps.package:
        shell.exec(*step.split(" "), cwd=str(build_dir))

    printDone()


def setupContainer(image: str) -> None:
    if shell.uname().sysname != "linux":
        tryCreateMachine()

    tryCreateContainer(image=image)


def doBuild(recipe: str, quiet: bool) -> None:
    if (recipeDir / f"{recipe}.json").exists():
        expr = jexpr.read(recipeDir / f"{recipe}.json")
        r = Recipe.from_dict(expr)
        fetchRecipe(r)
        buildRecipe(r, quiet)
    else:
        raise RuntimeError(f"No such recipe: {recipe}")


def wasRecipeBuilt(recipe: str) -> bool:
    return (cacheBuildsDir / f"{recipe}.built").exists()


@cli.command(None, "bootstrap", "Bootstrap distribution")
def _():
    pass


class BuildArgs(model.RegistryArgs):
    name: str = cli.arg(None, "recipe", "Recipe to build")
    in_container: bool = cli.arg(
        None, "in-container", "Whether or not this is run in the container"
    )
    quiet: bool = cli.arg(None, "quiet", "Whether or not to silence command output")


class BuildAllArgs(model.RegistryArgs):
    in_container: bool = cli.arg(
        None, "in-container", "Whether or not this is run in the container"
    )
    quiet: bool = cli.arg(None, "quiet", "Whether or not to silence command output")


class PatchArgs(model.RegistryArgs):
    recipe: str = cli.arg(None, "recipe", "Recipe to patch")


@cli.command(None, "bootstrap/build-all", "Build all packages")
def _(args: BuildAllArgs):
    if args.in_container:
        if recipeDir.exists():
            for file in recipeDir.iterdir():
                if wasRecipeBuilt(file.stem):
                    print(f"{file.stem}: no work to do")
                    continue

                doBuild(file.stem, args.quiet)

        else:
            raise RuntimeError("No 'recipes' directory!")
    else:
        runCutekitCommandInContainer(
            defaultContainerName, f"build-all --quiet={args.quiet}"
        )


@cli.command(None, "bootstrap/build", "Build a recipe")
def _(args: BuildArgs):
    if wasRecipeBuilt(args.name):
        print("No work to do")
        return

    if args.in_container:
        doBuild(args.name, args.quiet)
    else:
        runCutekitCommandInContainer(
            defaultContainerName, f"build --recipe={args.name} --quiet={args.quiet}"
        )


@cli.command(None, "bootstrap/rebuild", "Rebuild a recipe")
def _(args: BuildArgs):
    built_file = cacheBuildsDir / f"{args.name}.built"

    if wasRecipeBuilt(args.name):
        shell.rmrf(str(built_file))

    if args.in_container:
        if (recipeDir / f"{args.name}.json").exists():
            expr = jexpr.read(recipeDir / f"{args.name}.json")
            recipe = Recipe.from_dict(expr)

            fetchRecipe(recipe)
            buildRecipe(recipe, args.quiet)
        else:
            raise RuntimeError(f"No such recipe: {args.name}")
    else:
        runCutekitCommandInContainer(
            defaultContainerName, f"build --recipe={args.name} --quiet={args.quiet}"
        )


@cli.command(None, "bootstrap/make-patch", "Start the patching process")
def _(args: PatchArgs):
    sources_clean_dir = cacheSourcesDir / f"{args.recipe}-clean"

    # TODO: fetch if not fetched already
    if not sources_clean_dir.exists():
        raise RuntimeError("Recipe sources were not fetched yet")

    shell.cpTree(str(sources_clean_dir), f"{args.recipe}-workdir")
    print(
        f"Created new directory '{args.recipe}-workdir', make your changes and run 'save-patch'"
    )


@cli.command(None, "bootstrap/save-patch", "Save modifications into a patch")
def _(args: PatchArgs):
    workdir = Path.cwd() / f"{args.recipe}-workdir"
    sources_clean_dir = cacheSourcesDir / f"{args.recipe}-clean"

    proc = subprocess.run(
        ["git", "diff", "--no-index", "--no-prefix", sources_clean_dir, workdir],
        check=False,
        stdout=subprocess.PIPE,
    )

    with open(args.recipe + ".patch", "wb") as f:
        f.write(proc.stdout)

    print(f"Save patch to {args.recipe}.patch")

    remove = vt100.ask("Remove workdir directory?", default=True)

    if remove:
        shell.rmrf(str(workdir))


# TODO: Do container setup in 'build' if init wasn't called
@cli.command("i", "bootstrap/init", "Init container")
def _(args: InitArgs):
    if args.image not in IMAGES:
        raise RuntimeError(f"Invalid image, available options are {IMAGES}")

    setupContainer(IMAGES[args.image].id)
