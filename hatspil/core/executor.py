"""The module responsible for tasks execution.

This module contains the core mechanics of HaTSPiL. The execution of
each single operation is handled through the classes defined here, which
choose the correct sets of operations depending on the previous state
of the analysis and a set of customizable parameters.

For more information, see the `Execution` class.
"""

import os
import re
from enum import Enum
from typing import (Any, Callable, Dict, Iterable, List, Mapping, Optional,
                    Sequence, Tuple, Union, cast)

from ..config import KitData
from . import utils
from .analysis import Analysis
from .barcoded_filename import BarcodedFilename
from .exceptions import DataError, PipelineError


class AnalysisType(Enum):
    """The type of sample for a single file.

    Describe whether a file contains the data of a sample, a control or
    if it is unspecified.
    """

    Unspecified = 0
    Sample = 1
    Control = 2


class AnalysisFileData:
    """A helper class to cache some file information.

    This class stores some information that can be really useful when
    writing the code (or the command line) for an execution task. Each
    input file is an instance of an `AnalysisFileData` and it can be
    used to retrieve the following properties:
     * filename -- the actual file name.
     * barcode -- the associated barcode.
     * type -- the type, as instance of `AnalysisType`.

    This instance returns the filename when converted to `str`, in order
    to ease the creation of a command line string.
    """

    def __init__(self, filename: str) -> None:
        """Create an `AnalysisFileData`."""
        self.filename = filename
        try:
            self.barcode = BarcodedFilename(filename)
            if self.barcode.tissue.is_normal():
                self.type = AnalysisType.Control
            elif self.barcode.tissue.is_tumor():
                self.type = AnalysisType.Sample
            else:
                self.type = AnalysisType.Unspecified
        except Exception:
            self.type = AnalysisType.Unspecified

    def __repr__(self) -> str:
        """Return the `filename` property."""
        return self.filename


class SingleAnalysis(List[AnalysisFileData]):
    """A helper class to get control and sample for an analysis.

    In case of an analysis with a sample and a control, it is desirable
    to get the one or the other without parsing a list of two files.
    This class extends a list of `AnalysisFileData` in order to easily
    get the sample and the control for an analysis.
    """

    @property
    def sample(self) -> Optional[AnalysisFileData]:
        """Get the sample file."""
        return next(
            (file_data for file_data in self if file_data.type == AnalysisType.Sample),
            None,
        )

    @property
    def control(self) -> Optional[AnalysisFileData]:
        """Get the control file."""
        return next(
            (file_data for file_data in self if file_data.type == AnalysisType.Control),
            None,
        )


AnalysesPerOrganism = Dict[str, List[SingleAnalysis]]


class _ExecutorData:
    def __init__(
        self,
        command: Union[str, List[str], Callable[..., None]],
        output_format: Union[
            str, Callable[..., str], List[Union[str, Callable[..., str]]], None
        ],
        input_filenames: Optional[Sequence[str]],
        input_function: Optional[Callable[[Union[str, List[str]]], Optional[str]]],
        input_split_reads: bool,
        output_path: Optional[str],
        output_function: Optional[Callable[[str], Iterable[str]]],
        error_string: Optional[str],
        exception_string: Optional[str],
        override_last_files: bool,
        write_bam_files: bool,
        unlink_inputs: bool,
        save_only_last: bool,
        use_normals: bool,
        split_by_organism: bool,
        only_human: bool,
        split_input_files: bool,
        allow_raw_filenames: bool,
    ) -> None:

        self.command = command
        self.output_format = output_format
        self.input_filenames = input_filenames
        self.input_function = input_function
        self.input_split_reads = input_split_reads
        self.output_path = output_path
        self.output_function = output_function
        self.error_string = error_string
        self.exception_string = exception_string
        self.override_last_files = override_last_files
        self.write_bam_files = write_bam_files
        self.unlink_inputs = unlink_inputs
        self.save_only_last = save_only_last
        self.use_normals = use_normals
        self.split_by_organism = split_by_organism
        self.only_human = only_human
        self.split_input_files = split_input_files
        self.allow_raw_filenames = allow_raw_filenames


class Executor:
    """The class responsible for executing tasks.

    The execution core of HaTSPiL. The `Executor` class changes its
    behaviour depending on many aspects, for instance the files obtained
    from the last execution task and the parameters specified from the
    user.

    To use `Executor`, just create an instance and then call it with the
    desired parameters. For more information, see the `__call__` method.
    """

    RE_REPLACER = re.compile(r"\{([^}]+)\}")

    def __init__(self, analysis: Analysis) -> None:
        """Create a new instance."""
        self.analysis = analysis
        self.data: Optional[_ExecutorData] = None

    def _handle_output_filename(
        self,
        command_index: int,
        commands_len: int,
        organism: str,
        output_filename: Union[List[str], str],
        output_filenames: Dict[str, List[str]],
        output_bamfiles: Dict[str, List[str]],
    ) -> None:
        assert self.data

        if isinstance(output_filename, str):
            output_filename = [output_filename]

        new_filename: List[str] = []
        for filename in output_filename:
            dirname = os.path.dirname(filename)
            if dirname == "" or dirname == ".":
                new_filename.append(os.path.join(os.getcwd(), filename))
            else:
                new_filename.append(filename)
        output_filename = new_filename

        if not self.data.save_only_last or command_index == commands_len - 1:
            for filename in output_filename:
                if self.data.split_by_organism:
                    try:
                        output_organism = BarcodedFilename(filename).organism
                        if not output_organism:
                            output_organism = organism
                    except Exception:
                        output_organism = organism
                else:
                    output_organism = organism

                output_filenames.setdefault(output_organism, []).append(filename)
                output_extension = os.path.splitext(filename)[1]
                if output_extension == ".bam":
                    output_bamfiles.setdefault(output_organism, []).append(filename)

    def _get_output_filename(
        self, all_params: Mapping[str, Any]
    ) -> Union[List[str], str, None]:
        assert self.data

        locals().update(all_params)
        if self.data.output_format is not None:

            if isinstance(self.data.output_format, list):
                raw_output_formats = self.data.output_format[:]
            else:
                raw_output_formats = [self.data.output_format]

            output_formats: List[str] = []
            for raw_output_format in raw_output_formats:
                if isinstance(raw_output_format, str):
                    output_formats.append(raw_output_format)
                else:
                    # output_format is a Callable[..., str]
                    output_formats.append(raw_output_format(**all_params))

            if self.data.output_path is not None:
                output_formats = [
                    os.path.join(self.data.output_path, output_format)
                    for output_format in output_formats
                ]

            output_filename = []
            for s in output_formats:
                for match in Executor.RE_REPLACER.finditer(s):
                    try:
                        evaluated = eval(match.group(1))
                    except Exception:
                        raise PipelineError("cannot evaluate %s" % match.group(1))

                    if evaluated is None:
                        raise PipelineError("evaluation of %s is None" % match.group(1))
                    s = re.sub(match.group(0), str(evaluated), s)

                output_filename.append(s)

            if self.data.output_function is not None:
                output_filename = [
                    filename
                    for filenames in map(self.data.output_function, output_filename)
                    for filename in filenames
                ]

            if len(output_filename) == 1:
                return output_filename[0]
            else:
                return output_filename

        elif self.data.output_function is not None:
            input_filenames = all_params["input_filenames"]
            output_filenames = [
                filename
                for filenames in map(
                    self.data.output_function,
                    [filename.filename for filename in input_filenames],
                )
                for filename in filenames
            ]

            if len(output_filenames) == 1:
                return output_filenames[0]
            else:
                return output_filenames
        else:
            return None

    def _unlink_filename(
        self,
        input_filename: SingleAnalysis,
        real_input_filename: Optional[SingleAnalysis],
    ) -> None:
        assert self.data

        if (
            self.data.unlink_inputs
            and self.analysis.can_unlink
            and not self.analysis.run_fake
        ):
            if self.data.input_function is not None:
                assert real_input_filename
                input_filename = real_input_filename

            for file_data in input_filename:
                filename = file_data.filename
                extension = os.path.splitext(filename)[1].lower()
                os.unlink(filename)
                if extension == ".bam":
                    bai_file = filename[:-4] + ".bai"
                    if os.path.exists(bai_file):
                        os.unlink(bai_file)

    def _handle_command(
        self,
        current_command: Union[str, Callable[..., None]],
        all_params: Mapping[str, Any],
    ) -> None:
        assert self.data

        locals().update(all_params)
        if isinstance(current_command, str):
            if not self.analysis.run_fake:
                status = utils.run_and_log(current_command, self.analysis.logger)
            else:
                self.analysis.logger.info("Faking command '%s'", current_command)
                status = 0
        else:
            if not self.analysis.run_fake:
                current_command(**all_params)
            else:
                self.analysis.logger.info("Faking lambda")
            status = 0

        if status != 0:
            assert isinstance(self.data.command, str)
            arg_zero = os.path.basename(self.data.command.split(" ")[0])

            if self.data.error_string is None:
                error_string = "%s exited with status %d" % (arg_zero, status)
            else:
                error_string = self.data.error_string

            if self.data.exception_string is None:
                exception_string = "%s error" % arg_zero
            else:
                exception_string = self.data.exception_string

            for match in Executor.RE_REPLACER.finditer(error_string):
                try:
                    error_string = re.sub(
                        match.group(0), str(eval(match.group(1))), error_string
                    )
                except Exception:
                    raise PipelineError(
                        "cannot replace parameter %s" % (match.group(0))
                    )

            for match in Executor.RE_REPLACER.finditer(exception_string):
                try:
                    exception_string = re.sub(
                        match.group(0), str(eval(match.group(1))), exception_string
                    )
                except Exception:
                    raise PipelineError(
                        "cannot replace parameter %s" % (match.group(0))
                    )
            self.analysis.logger.error(error_string)
            raise PipelineError(exception_string)

    def _create_mod_input_filenames(
        self, input_filenames: AnalysesPerOrganism
    ) -> AnalysesPerOrganism:
        assert self.data
        assert self.data.input_function

        self._fix_input_filenames(input_filenames)

        mod_input_filenames: AnalysesPerOrganism = {}
        for organism, analyses in input_filenames.items():
            mod_analyses = mod_input_filenames.setdefault(organism, [])

            for analysis in analyses:
                filenames = [analysis_file.filename for analysis_file in analysis]

                if self.data.input_split_reads:
                    splitted_data: Dict[int, List[str]] = {}
                    for filename in filenames:
                        try:
                            barcoded = BarcodedFilename(filename)
                            if barcoded.read_index:
                                splitted_data.setdefault(
                                    barcoded.read_index, []
                                ).append(filename)
                            else:
                                splitted_data.setdefault(0, []).append(filename)
                        except Exception:
                            splitted_data.setdefault(0, []).append(filename)

                    for filenames in splitted_data.values():
                        param: Union[str, List[str]] = list(filenames)
                        if len(param) == 1:
                            param = param[0]

                        input_str = self.data.input_function(param)
                        if input_str:
                            mod_analyses.append(
                                SingleAnalysis([AnalysisFileData(input_str)])
                            )
                else:
                    result = cast(Callable[[List[str]], str], self.data.input_function)(
                        filenames
                    )
                    mod_analyses.append(SingleAnalysis([AnalysisFileData(result)]))

        if not mod_input_filenames:
            raise PipelineError("empty input list")

        self._fix_input_filenames(mod_input_filenames)
        return mod_input_filenames

    def _fix_input_filenames(self, input_filenames: AnalysesPerOrganism) -> None:
        assert self.data

        if not input_filenames:
            raise PipelineError("empty input list")

        if not self.data.input_split_reads:
            for organism, analyses in input_filenames.items():
                input_filenames[organism] = [
                    SingleAnalysis(
                        [file_data for analysis in analyses for file_data in analysis]
                    )
                ]

    @staticmethod
    def _get_fixed_normals_analyses(
        analyses: List[SingleAnalysis]
    ) -> List[SingleAnalysis]:

        for analysis_index, analysis in enumerate(analyses):
            if not analysis:
                continue

            file_data = next(iter(analysis))
            if file_data.type != AnalysisType.Sample:
                continue

            controls = [
                (other_analysis, other_file_data)
                for other_analysis in analyses
                for other_file_data in other_analysis
                if file_data.barcode.equals_without_tissue(other_file_data.barcode)
                and other_file_data.barcode.tissue.is_normal()
            ]

            if len(controls) == 1:
                control = controls[0]
                analysis.append(control[1])
                control[0].remove(control[1])
            else:
                sequencing_specific = [
                    control
                    for control in controls
                    if control[1].barcode.sequencing == file_data.barcode.sequencing
                ]

                if sequencing_specific:
                    controls = sequencing_specific

                analyses[analysis_index] = SingleAnalysis(
                    analysis + [control[1] for control in controls]
                )

                for control in controls:
                    control[0].remove(control[1])

        return [analysis for analysis in analyses if analysis]

    def _get_input_filenames(self) -> Tuple[AnalysesPerOrganism, AnalysesPerOrganism]:
        assert self.data

        if self.data.input_filenames is None:
            if self.analysis.last_operation_filenames is None:
                raise PipelineError(
                    "input files missing and last_operation_filenames empty"
                )

            raw_input_filenames = utils.get_sample_filenames(
                self.analysis.last_operation_filenames, self.data.split_by_organism
            )
        else:
            raw_input_filenames = utils.get_sample_filenames(
                self.data.input_filenames, self.data.split_by_organism
            )

        input_filenames: AnalysesPerOrganism = {}
        if isinstance(raw_input_filenames, dict):
            for organism, filenames in raw_input_filenames.items():
                analyses = input_filenames.setdefault(organism, [])
                if self.data.split_input_files:
                    for filename in filenames:
                        analyses.append(SingleAnalysis([AnalysisFileData(filename)]))
                else:
                    analyses.append(
                        SingleAnalysis(
                            [AnalysisFileData(filename) for filename in filenames]
                        )
                    )
        else:
            analyses = input_filenames.setdefault("", [])
            if self.data.split_input_files:
                for filename in raw_input_filenames:
                    analyses.append(SingleAnalysis([AnalysisFileData(filename)]))
            else:
                analyses.append(
                    SingleAnalysis(
                        [AnalysisFileData(filename) for filename in raw_input_filenames]
                    )
                )

        if self.analysis.parameters["use_normals"] and self.data.use_normals:
            for organism, analyses in input_filenames.items():
                input_filenames[organism] = Executor._get_fixed_normals_analyses(
                    analyses
                )

        if self.data.input_function is not None:
            return (input_filenames, self._create_mod_input_filenames(input_filenames))
        else:
            self._fix_input_filenames(input_filenames)
            return (input_filenames, {})

    def _get_additional_params(self, organism: Optional[str]) -> Dict[str, str]:
        additional_params = {}

        if not organism:
            additional_params["organism_str"] = ""
            organism = utils.get_human_annotation(self.analysis.config)
        else:
            additional_params["organism_str"] = "." + organism

        try:
            genome_ref, genome_index = utils.get_genome_ref_index_by_organism(
                self.analysis.config, organism
            )
            additional_params["genome_ref"] = genome_ref
            additional_params["genome_index"] = genome_index
        except DataError:
            pass

        try:
            additional_params["dbsnp"] = utils.get_dbsnp_by_organism(
                self.analysis.config, organism
            )
        except DataError:
            pass

        try:
            additional_params["cosmic"] = utils.get_cosmic_by_organism(
                self.analysis.config, organism
            )
        except DataError:
            pass

        return additional_params

    def _get_kit_additional_params(
        self, organism: Optional[str], kit: Optional[KitData]
    ) -> Dict[str, str]:
        additional_params = {}
        if not organism:
            organism = utils.get_human_annotation(self.analysis.config)

        if kit and organism.startswith("hg"):
            additional_params["indels"] = getattr(kit, "indels_{}".format(organism))

        return additional_params

    def _get_commands(
        self, all_params: Mapping[str, Any]
    ) -> List[Union[str, Callable[..., None]]]:
        assert self.data
        assert self.data.command

        locals().update(all_params)

        commands: List[Union[str, Callable[..., None]]] = []
        if isinstance(self.data.command, list):
            for s in self.data.command:
                if isinstance(s, str):
                    for match in Executor.RE_REPLACER.finditer(s):
                        try:
                            evaluated = eval(match.group(1))
                        except Exception:
                            raise PipelineError("cannot evaluate %s" % match.group(1))

                        if evaluated is None:
                            raise PipelineError(
                                "evaluation of %s is None" % (match.group(1))
                            )

                        s = s.replace(match.group(0), str(evaluated))

                commands.append(s)
        else:
            if isinstance(self.data.command, str):
                current_command = str(self.data.command)
                for match in Executor.RE_REPLACER.finditer(self.data.command):
                    try:
                        evaluated = eval(match.group(1))
                    except Exception:
                        raise PipelineError("cannot evaluate %s" % (match.group(1)))

                    if evaluated is None:
                        raise PipelineError(
                            "evaluation of %s is None" % (match.group(1))
                        )

                    current_command = current_command.replace(
                        match.group(0), str(evaluated)
                    )

                commands.append(current_command)
            else:
                commands.append(self.data.command)

        return commands

    def _handle_analysis(
        self,
        analysis_input: SingleAnalysis,
        mod_analysis_input: Optional[SingleAnalysis],
        output_filenames: Dict[str, List[str]],
        output_bamfiles: Dict[str, List[str]],
        analyses: AnalysesPerOrganism,
    ) -> None:
        assert self.data

        real_analysis_input: Optional[SingleAnalysis]
        if self.data.input_function is not None:
            assert mod_analysis_input

            real_analysis_input = analysis_input
            analysis_input = mod_analysis_input
        else:
            real_analysis_input = None

        input_filenames = analysis_input
        if len(analysis_input) == 1:
            input_filename = analysis_input[0]

        file_data = analysis_input.sample
        if not file_data:
            file_data = analysis_input.control

        if not self.data.allow_raw_filenames:
            assert file_data

        if file_data:
            organism = file_data.barcode.organism
            kit = utils.get_kit_from_barcoded(self.analysis.config, file_data.barcode)
        else:
            organism = None
            kit = None

        locals().update(self._get_kit_additional_params(organism, kit))

        if not self.data.only_human or not organism or organism.startswith("hg"):
            locals().update(self._get_additional_params(organism))
            if not organism:
                organism = utils.get_human_annotation(self.analysis.config)

            local_params = {
                key: value for key, value in locals().items() if key != "self"
            }
            output_filename = self._get_output_filename(local_params)

            local_params.update(
                {"output_filename": output_filename, "config": self.analysis.config}
            )
            commands = self._get_commands(local_params)

            for command_index, current_command in enumerate(commands):
                self._handle_command(
                    current_command,
                    {key: value for key, value in locals().items() if key != "self"},
                )

                if output_filename:
                    self._handle_output_filename(
                        command_index,
                        len(commands),
                        organism,
                        output_filename,
                        output_filenames,
                        output_bamfiles,
                    )

        self._unlink_filename(analysis_input, real_analysis_input)

    def __call__(
        self,
        command: Union[str, List[str], Callable[..., None]],
        output_format: Union[
            str, Callable[..., str], List[Union[str, Callable[..., str]]], None
        ] = None,
        input_filenames: Optional[Sequence[str]] = None,
        input_function: Optional[
            Callable[[Union[str, List[str]]], Optional[str]]
        ] = None,
        input_split_reads: bool = True,
        output_path: Optional[str] = None,
        output_function: Optional[Callable[[str], Iterable[str]]] = None,
        error_string: Optional[str] = None,
        exception_string: Optional[str] = None,
        override_last_files: bool = True,
        write_bam_files: bool = True,
        unlink_inputs: bool = False,
        save_only_last: bool = True,
        use_normals: bool = False,
        split_by_organism: bool = False,
        only_human: bool = False,
        split_input_files: bool = True,
        allow_raw_filenames: bool = False,
    ) -> None:
        """Use the Executor to perform a task.

        When you need to use an Executor, just call it.
        Executor is able to handle both strings, handled as command line
        arguments, and python functions.

        Except for `command`, all of the arguments are optional, but
        they allow a fine grained control of the execution behaviour.

        Args:
            command: the command that is executed. If it is a `str`, a
                     new process is created, using the value of
                     `command` as command line argument. Moreover, the
                     can contain "evaluable arguments", which is
                     replaced with the actual evaluation before calling
                     the command. The format to specify these evaluable
                     arguments are the same as the string format
                     literals, a pair of curly braces with the
                     evaluation string inside. For instance,
                     "Hello {world_str}" has a pair of curly braces
                     which are replaced with the value of the variable
                     `world_str`. If the current value of `world_str` is
                     "World!", the whole string becomes "Hello World!".
                     There are many variables that can be evaluated
                     by Executor:
                        input_filenames: an instance of `SingleAnalysis`
                        input_filename: in case `input_filenames` is a
                            list of a single element, `input_filename`
                            can be used to refer to that single element.
                            WARNING: if the list contains a number of
                            elements different than 1, `input_filename`
                            is undefined. Use with care!
                        real_analysis_input: when the `input_function`
                            parameter (see below) is not None, the
                            `input_filenames` can be different from the
                            output filename of the last execution task.
                            `real_analysis_input` contains the values
                            before `input_function` is called.
                        output_filename: a `str`, a list of `str` or
                            None. If both `output_function` and
                            `output_format` parameters are `None`,
                            `output_filename` is `None`. Otherwise, if
                            `output_format` and/or `output_filename`
                            return a list of strings with more than one
                            element, `output_filename` is a `str`,
                            otherwise a list of `str`. See
                            `output_function` and `output_format`
                            parameter's documentation for more
                            information.
                        organism: the organism (and genome annotation
                            version) for the current sample. Possible
                            values are 'hg19' and 'hg38'. The value is
                            obtained from the barcode of the sample. See
                            `organism_str` for more information.
                        organism_str: the organism string that can be
                            used to distinguish a file from another.
                            If the organism string is available from the
                            sample barcode, it is used with a '.' at the
                            beginning (i.e.: '.hg19'). Otherwise, when
                            the organism cannot be detected from the
                            barcode, the default human annotation is
                            used for the `organism` variable, and
                            `organism_str` is empty.
                        kit: a `config.KitData` instance (or None if
                            not available) for the current sample. See
                            `utils.get_kit_from_barcoded` and
                            `config.KitData` for more information.
                        indels: a helper variable obtained from the
                            `kit` using the current `organism`.
                        genome_ref: the genome reference file for the
                            current organism. For instance, if the
                            current organism is 'hg19', `genome_ref` has
                            the same value as `config.hg19_ref`.
                        genome_index: the genome index file prefix for
                            the current organism. For instance, if the
                            current organism is 'hg19', `genome_index`
                            has the same value as `config.hg19_index`.
                        dbsnp: the `dbsnp` for the current organism,
                            obtained from `config`.
                        cosmic: the `cosmic` for the current organism,
                            obtined from `config`.
                        config: the current `config` instance.
                    If `command` is a python function, the variables are
                    passed to the function as kwargs.
            output_format: specifies the format for the output
                           filename(s). It uses the same system of
                           evaluable arguments as documented for
                           `command`. If `output_format` is a python
                           function, the variables documented for the
                           `command` parameter are passed as kwargs.
                           If `output_format` is a list of `str` or
                           a list of python functions, a list of
                           output files will be available for the
                           `output_filename` variable instead of a
                           `str`. This parameter combines with the
                           `output_function` parameter (see below).
            input_filenames: override the last execution task with a
                             list of input files.
            input_function: a python function that is applied to the
                            input filenames before creating the command.
                            In case the parameter `input_split_reads` is
                            `True`, the argument of the function can be
                            a `str` or a list of `str`. Otherwise it
                            will always be a list of `str`. See
                            `input_split_reads` arguments for more
                            information.
            input_split_reads: determines if multiple commands must be
                               run or not when multiple read indices are
                               available from the input files. For
                               instance, in some situations a command
                               must be run separately for both
                               ".R1.fastq" and ".R2.fastq". For this
                               purpose the `input_split_reads` parameter
                               must be set to `True`.
            output_path: helper parameter to automatically prepend a
                         path to the results of the evaluation of the
                         `output_format` parameter. In case
                         `output_format` is `None`, this parameter does
                         not have any effect.
            output_function: a python function to create the output
                             filename procedurally. In case
                             `output_format` is set, the function will
                             be called for each result from the format
                             output. Otherwise, the function will be
                             called for each input filename.
            error_string: a custom string that will be outputted in case
                          the exit status of a command is not zero.
            exception_string: a custom string that will be embedded in
                              the exception thrown in case the exit
                              status of a command is not zero.
            override_last_files: if the execution task output filename
                                 must be updated or not. When this value
                                 is `True`, the next execution step will
                                 have the current output files as input
                                 files. Otherwise, the next execution
                                 task will have the same input files as
                                 the current execution.
            write_bam_files: in case BAM files are available in the
                             output filenames, this parameter determines
                             if the analysis bamfiles must be updated
                             accordingly or not.
            unlink_inputs: determines if the input files must be deleted
                           from hard disk or not. Note that this
                           parameter will not have any effects if
                           `analysis.can_unlink` is set (i.e. the
                           Starter.run method avoid the deletion of the
                           input files using this method).
            save_only_last: determines if only the last output filename
                            must be saved for the following execution
                            tasks.
            use_normals: when set, the control files are handled
                         together with sample file within the same
                         execution unit. If the parameter is `False` or
                         the use of normals is disabled from command
                         line, the control files are handled separately
                         from the relative sample files.
            split_by_organism: specifies if the input files must be
                               split by organism or not. See
                               `utils.get_sample_filenames` for more
                               information.
            only_human: ignore all the samples that are not classified
                        as human. It is worth noting that a sample that
                        does not have an organism field is classified
                        as human sample.
            split_input_files: specifies if, for each organism, each set
                               of input files must be handled among
                               different execution units or not. In
                               detail, if `split_input_files` is `True`,
                               many `SingleAnalysis` with one input
                               filename are created. Otherwise, one
                               single analysis is created with all the
                               relative input filenames.
            allow_raw_filenames: if this parameter is set, it is
                                 possible to use input filenames with
                                 invalid barcodes. In edge cases, it is
                                 useful to use the `Executor` to perform
                                 simple tasks that involve filenames
                                 that cannot be barcoded by design.

        """
        self.data = _ExecutorData(
            command,
            output_format,
            input_filenames,
            input_function,
            input_split_reads,
            output_path,
            output_function,
            error_string,
            exception_string,
            override_last_files,
            write_bam_files,
            unlink_inputs,
            save_only_last,
            use_normals,
            split_by_organism,
            only_human,
            split_input_files,
            allow_raw_filenames,
        )

        _input_filenames, mod_input_filenames = self._get_input_filenames()

        output_filenames: Dict[str, List[str]] = {}
        output_bamfiles: Dict[str, List[str]] = {}
        for current_organism in _input_filenames.keys():
            current_input_filenames = _input_filenames[current_organism]

            iterator: Union[
                Iterable[Tuple[SingleAnalysis, SingleAnalysis]],
                Iterable[Tuple[SingleAnalysis, None]],
            ]
            if input_function is not None:
                current_mod_input_filenames = mod_input_filenames[current_organism]
                if len(current_mod_input_filenames) == len(current_input_filenames):
                    iterator = zip(current_input_filenames, current_mod_input_filenames)
                else:
                    iterator = zip(
                        current_mod_input_filenames, current_mod_input_filenames
                    )
            else:
                iterator = zip(
                    current_input_filenames, [None] * len(current_input_filenames)
                )

            for input_filename, mod_input_filename in iterator:
                self._handle_analysis(
                    input_filename,
                    mod_input_filename,
                    output_filenames,
                    output_bamfiles,
                    _input_filenames,
                )

        if override_last_files:
            self.analysis.last_operation_filenames = output_filenames
            self.analysis.can_unlink = True

        if write_bam_files and len(output_bamfiles) != 0:
            self.analysis.bamfiles = output_bamfiles

    def override_last_operation_filename(self, new_filename: str) -> None:
        """Override the last operation filenames. DEPRECATED.
        
        Please use the executor capabilities to avoid the situations in
        which it is necessary to use this function or to manually modify
        the `analysis.last_operation_filenames` parameter. It is
        extremely dangerous and it can lead to many strange-behaving
        edge cases.
        """
        if not self.analysis.last_operation_filenames:
            raise PipelineError("last operation did not leave an output file")

        if isinstance(self.analysis.last_operation_filenames, str):
            self.analysis.last_operation_filenames = new_filename
        elif isinstance(self.analysis.last_operation_filenames, list):
            if len(self.analysis.last_operation_filenames) != 1:
                raise PipelineError(
                    "last operation created a list with a number of output "
                    "files different than one"
                )

            self.analysis.last_operation_filenames[0] = new_filename
        elif isinstance(self.analysis.last_operation_filenames, dict):
            if len(self.analysis.last_operation_filenames) != 1:
                raise PipelineError(
                    "last operation created a dict using more than one organism"
                )

            organism, last_filenames = next(
                iter(self.analysis.last_operation_filenames.items())
            )
            if isinstance(last_filenames, str):
                self.analysis.last_operation_filenames[organism] = new_filename
            elif isinstance(last_filenames, list):
                if len(last_filenames) != 1:
                    raise PipelineError(
                        "last operation created a dict of lists with one "
                        "list, but the list contains a number of filenames "
                        "different than one"
                    )
                self.analysis.last_operation_filenames[organism][0] = new_filename
            else:
                raise PipelineError("last operation created an invalid dict")
        else:
            raise PipelineError("last operation created an invalid object")
