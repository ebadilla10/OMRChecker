"""

 OMRChecker

 Author: Udayraj Deshmukh
 Github: https://github.com/Udayraj123

"""
import os
from dataclasses import dataclass
from csv import QUOTE_NONNUMERIC
from pathlib import Path
from time import time

import cv2
import numpy as np
import pandas as pd
from rich.table import Table

from src.constants.common import (
    CONFIG_FILENAME,
    ERROR_CODES,
    EVALUATION_FILENAME,
    TEMPLATE_FILENAME,
)
from src.defaults import CONFIG_DEFAULTS
from src.evaluation import EvaluationConfig, evaluate_concatenated_response
from src.logger import console, logger
from src.template import Template
from src.utils.file import Paths, setup_dirs_for_paths, setup_outputs_for_template
from src.utils.image import ImageUtils
from src.utils.interaction import InteractionUtils, Stats
from src.utils.parsing import get_concatenated_response, open_config_with_defaults

# Load processors
STATS = Stats()

try:
    import fitz
except ImportError:
    fitz = None


@dataclass
class OMRInput:
    source_path: Path
    display_path: str
    file_name: str
    image: object = None


def render_pdf_inputs(pdf_path, tuning_config):
    if fitz is None:
        raise Exception(
            "PyMuPDF is required to process PDF files. Install the 'PyMuPDF' package."
        )

    processing_width = tuning_config.dimensions.processing_width
    processing_height = tuning_config.dimensions.processing_height
    omr_inputs = []

    with fitz.open(pdf_path) as pdf_doc:
        for page_index in range(pdf_doc.page_count):
            page = pdf_doc[page_index]
            matrix = fitz.Matrix(
                processing_width / page.rect.width,
                processing_height / page.rect.height,
            )
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            encoded = np.frombuffer(pixmap.tobytes("png"), dtype=np.uint8)
            image = cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE)
            if image is None:
                raise Exception(f"Failed to render PDF page {page_index + 1}: {pdf_path}")

            file_name = (
                f"{pdf_path.stem}_p{page_index + 1}.png"
                if pdf_doc.page_count > 1
                else f"{pdf_path.stem}.png"
            )
            omr_inputs.append(
                OMRInput(
                    source_path=pdf_path,
                    display_path=f"{pdf_path}#page={page_index + 1}",
                    file_name=file_name,
                    image=image,
                )
            )
    return omr_inputs


def collect_omr_inputs(curr_dir, tuning_config):
    image_exts = ("*.[pP][nN][gG]", "*.[jJ][pP][gG]", "*.[jJ][pP][eE][gG]")
    omr_inputs = [
        OMRInput(source_path=f, display_path=str(f), file_name=f.name)
        for ext in image_exts
        for f in curr_dir.glob(ext)
    ]

    pdf_inputs = []
    for pdf_path in sorted(curr_dir.glob("*.[pP][dD][fF]")):
        pdf_inputs.extend(render_pdf_inputs(pdf_path, tuning_config))

    return sorted(omr_inputs, key=lambda omr_input: omr_input.file_name) + pdf_inputs


def load_omr_image(omr_input):
    if omr_input.image is not None:
        return omr_input.image.copy()
    return cv2.imread(str(omr_input.source_path), cv2.IMREAD_GRAYSCALE)


def get_template_asset_exclusions(curr_dir):
    tex_stems = {tex_path.stem for tex_path in curr_dir.glob("*.tex")}
    if not tex_stems:
        return []
    return [
        pdf_path
        for pdf_path in curr_dir.glob("*.[pP][dD][fF]")
        if pdf_path.stem in tex_stems
    ]


def entry_point(input_dir, args):
    if not os.path.exists(input_dir):
        raise Exception(f"Given input directory does not exist: '{input_dir}'")
    curr_dir = input_dir
    return process_dir(input_dir, curr_dir, args)


def print_config_summary(
    curr_dir,
    omr_files,
    template,
    tuning_config,
    local_config_path,
    evaluation_config,
    args,
):
    logger.info("")
    table = Table(title="Current Configurations", show_header=False, show_lines=False)
    table.add_column("Key", style="cyan", no_wrap=True)
    table.add_column("Value", style="magenta")
    table.add_row("Directory Path", f"{curr_dir}")
    table.add_row("Count of Images", f"{len(omr_files)}")
    table.add_row("Set Layout Mode ", "ON" if args["setLayout"] else "OFF")
    pre_processor_names = [pp.__class__.__name__ for pp in template.pre_processors]
    table.add_row(
        "Markers Detection",
        "ON" if "CropOnMarkers" in pre_processor_names else "OFF",
    )
    table.add_row("Auto Alignment", f"{tuning_config.alignment_params.auto_align}")
    table.add_row("Detected Template Path", f"{template}")
    if local_config_path:
        table.add_row("Detected Local Config", f"{local_config_path}")
    if evaluation_config:
        table.add_row("Detected Evaluation Config", f"{evaluation_config}")

    table.add_row(
        "Detected pre-processors",
        ", ".join(pre_processor_names),
    )
    console.print(table, justify="center")


def process_dir(
    root_dir,
    curr_dir,
    args,
    template=None,
    tuning_config=CONFIG_DEFAULTS,
    evaluation_config=None,
):
    # Update local tuning_config (in current recursion stack)
    local_config_path = curr_dir.joinpath(CONFIG_FILENAME)
    if os.path.exists(local_config_path):
        tuning_config = open_config_with_defaults(local_config_path)

    # Update local template (in current recursion stack)
    local_template_path = curr_dir.joinpath(TEMPLATE_FILENAME)
    local_template_exists = os.path.exists(local_template_path)
    if local_template_exists:
        template = Template(
            local_template_path,
            tuning_config,
        )
    # Look for subdirectories for processing
    subdirs = [d for d in curr_dir.iterdir() if d.is_dir()]

    output_dir = Path(args["output_dir"], curr_dir.relative_to(root_dir))
    paths = Paths(output_dir)

    # look for images/PDF pages in current dir to process
    omr_files = collect_omr_inputs(curr_dir, tuning_config)

    # Exclude images (take union over all pre_processors)
    excluded_files = []
    if template:
        for pp in template.pre_processors:
            excluded_files.extend(Path(p) for p in pp.exclude_files())
        excluded_files.extend(get_template_asset_exclusions(curr_dir))

    local_evaluation_path = curr_dir.joinpath(EVALUATION_FILENAME)
    if not args["setLayout"] and os.path.exists(local_evaluation_path):
        if not local_template_exists:
            logger.warning(
                f"Found an evaluation file without a parent template file: {local_evaluation_path}"
            )
        evaluation_config = EvaluationConfig(
            curr_dir,
            local_evaluation_path,
            template,
            tuning_config,
        )

        excluded_files.extend(
            Path(exclude_file) for exclude_file in evaluation_config.get_exclude_files()
        )

    omr_files = [f for f in omr_files if f.source_path not in excluded_files]

    if omr_files:
        if not template:
            logger.error(
                f"Found images, but no template in the directory tree \
                of '{curr_dir}'. \nPlace {TEMPLATE_FILENAME} in the \
                appropriate directory."
            )
            raise Exception(
                f"No template file found in the directory tree of {curr_dir}"
            )

        setup_dirs_for_paths(paths)
        outputs_namespace = setup_outputs_for_template(paths, template)

        print_config_summary(
            curr_dir,
            omr_files,
            template,
            tuning_config,
            local_config_path,
            evaluation_config,
            args,
        )
        if args["setLayout"]:
            show_template_layouts(omr_files, template, tuning_config)
        else:
            process_files(
                omr_files,
                template,
                tuning_config,
                evaluation_config,
                outputs_namespace,
            )

    elif not subdirs:
        # Each subdirectory should have images or should be non-leaf
        logger.info(
            f"No valid images or sub-folders found in {curr_dir}.\
            Empty directories not allowed."
        )

    # recursively process sub-folders
    for d in subdirs:
        process_dir(
            root_dir,
            d,
            args,
            template,
            tuning_config,
            evaluation_config,
        )


def show_template_layouts(omr_files, template, tuning_config):
    for omr_input in omr_files:
        file_name = omr_input.file_name
        file_path = omr_input.display_path
        in_omr = load_omr_image(omr_input)
        if in_omr is None:
            logger.error(f"Failed to open image for layout preview: '{file_path}'")
            continue
        in_omr = template.image_instance_ops.apply_preprocessors(
            file_path, in_omr, template
        )
        template_layout = template.image_instance_ops.draw_template_layout(
            in_omr, template, shifted=False, border=2
        )
        InteractionUtils.show(
            f"Template Layout: {file_name}", template_layout, 1, 1, config=tuning_config
        )


def process_files(
    omr_files,
    template,
    tuning_config,
    evaluation_config,
    outputs_namespace,
):
    start_time = int(time())
    files_counter = 0
    STATS.files_not_moved = 0

    for omr_input in omr_files:
        files_counter += 1
        file_path = omr_input.source_path
        display_path = omr_input.display_path
        file_name = omr_input.file_name

        in_omr = load_omr_image(omr_input)

        logger.info("")
        if in_omr is None:
            logger.error(f"({files_counter}) Failed to open image: '{display_path}'")
        else:
            logger.info(
                f"({files_counter}) Opening image: \t'{display_path}'\tResolution: {in_omr.shape}"
            )

        if in_omr is None:
            new_file_path = outputs_namespace.paths.errors_dir.joinpath(file_name)
            outputs_namespace.OUTPUT_SET.append(
                [file_name] + outputs_namespace.empty_resp
            )
            if check_and_move(ERROR_CODES.NO_MARKER_ERR, file_path, new_file_path):
                err_line = [
                    file_name,
                    display_path,
                    new_file_path,
                    "NA",
                ] + outputs_namespace.empty_resp
                pd.DataFrame(err_line, dtype=str).T.to_csv(
                    outputs_namespace.files_obj["Errors"],
                    mode="a",
                    quoting=QUOTE_NONNUMERIC,
                    header=False,
                    index=False,
                )
            continue

        template.image_instance_ops.reset_all_save_img()

        template.image_instance_ops.append_save_img(1, in_omr)

        in_omr = template.image_instance_ops.apply_preprocessors(
            display_path, in_omr, template
        )

        if in_omr is None:
            # Error OMR case
            new_file_path = outputs_namespace.paths.errors_dir.joinpath(file_name)
            outputs_namespace.OUTPUT_SET.append(
                [file_name] + outputs_namespace.empty_resp
            )
            if check_and_move(ERROR_CODES.NO_MARKER_ERR, file_path, new_file_path):
                err_line = [
                    file_name,
                    display_path,
                    new_file_path,
                    "NA",
                ] + outputs_namespace.empty_resp
                pd.DataFrame(err_line, dtype=str).T.to_csv(
                    outputs_namespace.files_obj["Errors"],
                    mode="a",
                    quoting=QUOTE_NONNUMERIC,
                    header=False,
                    index=False,
                )
            continue

        # uniquify
        file_id = str(file_name)
        save_dir = outputs_namespace.paths.save_marked_dir
        (
            response_dict,
            final_marked,
            multi_marked,
            _,
        ) = template.image_instance_ops.read_omr_response(
            template, image=in_omr, name=file_id, save_dir=save_dir
        )

        # TODO: move inner try catch here
        # concatenate roll nos, set unmarked responses, etc
        omr_response = get_concatenated_response(response_dict, template)

        if (
            evaluation_config is None
            or not evaluation_config.get_should_explain_scoring()
        ):
            logger.info(f"Read Response: \n{omr_response}")

        score = 0
        if evaluation_config is not None:
            score = evaluate_concatenated_response(
                omr_response,
                evaluation_config,
                display_path,
                outputs_namespace.paths.evaluation_dir,
            )
            logger.info(
                f"(/{files_counter}) Graded with score: {round(score, 2)}\t for file: '{file_id}'"
            )
        else:
            logger.info(f"(/{files_counter}) Processed file: '{file_id}'")

        if tuning_config.outputs.show_image_level >= 2:
            InteractionUtils.show(
                f"Final Marked Bubbles : '{file_id}'",
                ImageUtils.resize_util_h(
                    final_marked, int(tuning_config.dimensions.display_height * 1.3)
                ),
                1,
                1,
                config=tuning_config,
            )

        resp_array = []
        for k in template.output_columns:
            resp_array.append(omr_response[k])

        outputs_namespace.OUTPUT_SET.append([file_name] + resp_array)

        if multi_marked == 0 or not tuning_config.outputs.filter_out_multimarked_files:
            STATS.files_not_moved += 1
            new_file_path = save_dir.joinpath(file_id)
            # Enter into Results sheet-
            results_line = [file_name, display_path, new_file_path, score] + resp_array
            # Write/Append to results_line file(opened in append mode)
            pd.DataFrame(results_line, dtype=str).T.to_csv(
                outputs_namespace.files_obj["Results"],
                mode="a",
                quoting=QUOTE_NONNUMERIC,
                header=False,
                index=False,
            )
        else:
            # multi_marked file
            logger.info(f"[{files_counter}] Found multi-marked file: '{file_id}'")
            new_file_path = outputs_namespace.paths.multi_marked_dir.joinpath(file_name)
            if check_and_move(ERROR_CODES.MULTI_BUBBLE_WARN, file_path, new_file_path):
                mm_line = [file_name, display_path, new_file_path, "NA"] + resp_array
                pd.DataFrame(mm_line, dtype=str).T.to_csv(
                    outputs_namespace.files_obj["MultiMarked"],
                    mode="a",
                    quoting=QUOTE_NONNUMERIC,
                    header=False,
                    index=False,
                )
            # else:
            #     TODO:  Add appropriate record handling here
            #     pass

    print_stats(start_time, files_counter, tuning_config)


def check_and_move(error_code, file_path, filepath2):
    # TODO: fix file movement into error/multimarked/invalid etc again
    STATS.files_not_moved += 1
    return True


def print_stats(start_time, files_counter, tuning_config):
    time_checking = max(1, round(time() - start_time, 2))
    log = logger.info
    log("")
    log(f"{'Total file(s) moved': <27}: {STATS.files_moved}")
    log(f"{'Total file(s) not moved': <27}: {STATS.files_not_moved}")
    log("--------------------------------")
    log(
        f"{'Total file(s) processed': <27}: {files_counter} ({'Sum Tallied!' if files_counter == (STATS.files_moved + STATS.files_not_moved) else 'Not Tallying!'})"
    )

    if tuning_config.outputs.show_image_level <= 0:
        log(
            f"\nFinished Checking {files_counter} file(s) in {round(time_checking, 1)} seconds i.e. ~{round(time_checking / 60, 1)} minute(s)."
        )
        log(
            f"{'OMR Processing Rate': <27}: \t ~ {round(time_checking / files_counter, 2)} seconds/OMR"
        )
        log(
            f"{'OMR Processing Speed': <27}: \t ~ {round((files_counter * 60) / time_checking, 2)} OMRs/minute"
        )
    else:
        log(f"\n{'Total script time': <27}: {time_checking} seconds")

    if tuning_config.outputs.show_image_level <= 1:
        log(
            "\nTip: To see some awesome visuals, open config.json and increase 'show_image_level'"
        )
