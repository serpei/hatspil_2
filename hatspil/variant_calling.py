"""Module to handle a custom variant calling procedure."""
import glob
import math
import os
import re
from typing import Any, Dict, List, Optional

import pandas as pd
import vcf

from .core import utils
from .core.analysis import Analysis
from .core.barcoded_filename import Analyte, BarcodedFilename
from .core.exceptions import PipelineError
from .core.executor import Executor
from .core.ranges import GenomicRange, GenomicRanges
from .db import Db


class VariantCalling:
    """A class to produce meaningful variant calling results.

    This is a custom approach to integrate the results from the software
    used for the detection of the mutations and obtain a valuable set
    of genetic variations.
    
    This class contains a Python port of CeredaR R package
    (https://github.com/matteocereda/ceredaR/).
    """

    min_allele_frequency = 0.01
    min_cov_position = 10
    dataset_filename = os.path.join(os.path.dirname(__file__), "data.hdf")
    medium_damage = 5
    high_damage = 20
    strelka_tier = 0

    def __init__(self, analysis: Analysis) -> None:
        """Create an instance of the class.
        
        The variant calling output directory and a temporary directory
        for ANNOVAR are created if they do not exist.
        """
        self.analysis = analysis

        os.makedirs(analysis.get_out_dir(), exist_ok=True)
        self.mutect_filenames = glob.glob(
            os.path.join(analysis.get_out_dir(), self.analysis.basename)
            + "*.mutect*.vcf"
        )
        self.varscan_filenames: Dict[str, List[str]] = {}
        for varscan_type in ("snp", "indel"):
            self.varscan_filenames[varscan_type] = glob.glob(
                os.path.join(analysis.get_out_dir(), self.analysis.basename)
                + "*.varscan2."
                + varscan_type
                + "*.vcf"
            )
        self.annovar_dirname = os.path.join(
            analysis.get_out_dir(), self.analysis.basename + "_annovar"
        )

        self.strelka_results_dir = os.path.join(
            analysis.root, "Strelka", analysis.basename, "results"
        )

        os.makedirs(self.annovar_dirname, exist_ok=True)
        self.build_version = utils.get_human_annotation(self.analysis.config)

        self.annovar_file = os.path.join(
            self.annovar_dirname, self.analysis.basename + "_annovar_input"
        )
        self.variants_filename = os.path.join(
            self.annovar_dirname, self.analysis.basename + "_variants.csv"
        )
        self.multianno_filename = os.path.join(
            "%s.%s_multianno.txt" % (self.annovar_file, self.build_version)
        )

    def chdir(self) -> None:
        """Change current directory to the variant calling folder."""
        os.chdir(self.analysis.get_out_dir())

    def prepare_for_annovar(self) -> None:
        """Prepare all the data for ANNOVAR.

        The data produced by MuTect, VarScan and Strelka is collected
        and filtered in order to produce an input file for ANNOVAR.
        """
        self.analysis.logger.info("Starting data preparation for ANNOVAR")

        self.variants = None

        mutect_data: Optional[pd.Table] = None
        varscan_data: Optional[pd.Table] = None
        strelka_data: Optional[pd.Table] = None

        if len(self.mutect_filenames) > 0:
            for mutect_filename in self.mutect_filenames:
                if mutect_data is None:
                    mutect_data = pd.read_table(mutect_filename, comment="#")
                else:
                    mutect_data = mutect_data.join(
                        pd.read_table(mutect_filename, comment="#")
                    )

            assert mutect_data is not None
            mutect_data.insert(
                0,
                "key",
                mutect_data.apply(
                    lambda row: "%s:%d-%d_%s_%s"
                    % (
                        row.contig,
                        row.position,
                        row.position,
                        row.ref_allele,
                        row.alt_allele,
                    ),
                    axis=1,
                ),
            )

            mutect_data.drop(
                mutect_data[
                    mutect_data.tumor_f < VariantCalling.min_allele_frequency
                ].index,
                inplace=True,
            )
            mutect_data.drop(
                mutect_data[
                    (mutect_data.judgement == "REJECT")
                    & (mutect_data.failure_reasons != "possible_contamination")
                ].index,
                inplace=True,
            )
            mutect_data.drop(
                mutect_data[
                    mutect_data.strand_bias_counts.str.split(r"\W")
                    .map(lambda values: [int(value) for value in values if value != ""])
                    .map(lambda values: (values[2] <= 0 or values[3] <= 0))
                ].index,
                inplace=True,
            )
            mutect_data["tot_cov"] = mutect_data.t_ref_count + mutect_data.t_alt_count
            mutect_data.drop(
                mutect_data[
                    mutect_data.tot_cov <= VariantCalling.min_cov_position
                ].index,
                inplace=True,
            )

        for varscan_type in ("snp", "indel"):
            filenames = self.varscan_filenames[varscan_type]
            if len(filenames) > 0:
                withNormals = True
                varscan_data_list: List[Dict[str, Any]] = []
                for varscan_filename in filenames:
                    for record in vcf.Reader(filename=varscan_filename):
                        for sample in record.samples:
                            sample_data = {
                                "chr": record.CHROM,
                                "start": record.start + 1,
                                "end": record.end,
                                "width": record.end - record.start,
                                "ref": str(record.REF),
                                "alt": ",".join([str(alt) for alt in record.ALT]),
                                "totalDepth": sample.data.DP,
                                "refDepth": sample.data.RD,
                                "altDepth": sample.data.AD,
                                "indelError": "indelError" not in record.FILTER,
                                "QUAL": record.QUAL,
                                "GT": sample.data.GT,
                                "GQ": sample.data.GQ,
                                "RD": sample.data.RD,
                                "FREQ": float(sample.data.FREQ[:-1]) / 100,
                            }

                            if hasattr(sample.data, "DP4"):
                                if not withNormals:
                                    raise PipelineError(
                                        "mixed varscan data with/without normals"
                                    )
                                dp4 = sample.data.DP4.split(",")
                                sample_data.update(
                                    {
                                        "sampleNames": sample.sample,
                                        "DP": record.INFO["DP"],
                                        "SOMATIC": "SOMATIC" in record.INFO
                                        and record.INFO["SOMATIC"],
                                        "SS": record.INFO["SS"],
                                        "SSC": record.INFO["SSC"],
                                        "GPV": record.INFO["GPV"],
                                        "SPV": record.INFO["SPV"],
                                        "DP4": int(dp4[0]),
                                        "ADF": int(dp4[2]),
                                        "ADR": int(dp4[3]),
                                    }
                                )
                            else:
                                if withNormals:
                                    if len(varscan_data_list) != 0:
                                        raise PipelineError(
                                            "mixed varscan data with/without normals"
                                        )
                                    withNormals = False

                                sample_data.update(
                                    {
                                        "sampleNames": "TUMOR",
                                        "DP": None,
                                        "SOMATIC": None,
                                        "SS": None,
                                        "SSC": None,
                                        "GPV": None,
                                        "SPV": None,
                                        "DP4": None,
                                        "ADF": sample.data.ADF,
                                        "ADR": sample.data.ADR,
                                    }
                                )

                            varscan_data_list.append(sample_data)

                current_data = pd.DataFrame(
                    data=varscan_data_list,
                    columns=[
                        "chr",
                        "start",
                        "end",
                        "width",
                        "ref",
                        "alt",
                        "totalDepth",
                        "refDepth",
                        "altDepth",
                        "sampleNames",
                        "indelError",
                        "QUAL",
                        "DP",
                        "SOMATIC",
                        "SS",
                        "SSC",
                        "GPV",
                        "SPV",
                        "GT",
                        "GQ",
                        "RD",
                        "FREQ",
                        "DP4",
                        "ADF",
                        "ADR",
                    ],
                )
                current_data.drop_duplicates(inplace=True)
                if current_data.empty:
                    continue

                current_data["key"] = current_data.apply(
                    lambda row: "%s:%d-%d_%s_%s"
                    % (row.chr, row.start, row.end, row.ref, row.alt),
                    axis=1,
                )
                tumor = current_data[current_data.sampleNames == "TUMOR"].copy()
                tumor.reset_index(inplace=True, drop=True)

                if withNormals:
                    normal = current_data[current_data.sampleNames == "NORMAL"]
                    normal.reset_index(inplace=True, drop=True)

                    normals = tumor.merge(normal, on="key", how="left")[
                        ["totalDepth_x", "refDepth_x", "altDepth_x"]
                    ]
                    tumor["totalDepth.NORMAL"] = normals["totalDepth_x"].values
                    tumor["refDepth.NORMAL"] = normals["refDepth_x"].values
                    tumor["altDepth.NORMAL"] = normals["altDepth_x"].values

                if withNormals:
                    tumor.drop(
                        tumor[
                            (~tumor.indelError)
                            | (~tumor.SOMATIC)
                            | (tumor.DP < VariantCalling.min_cov_position)
                            | (tumor.FREQ < VariantCalling.min_allele_frequency)
                            | (tumor.ADF <= 0)
                            | (tumor.ADR <= 0)
                        ].index,
                        inplace=True,
                    )
                else:
                    drop_condition = (
                        (~tumor.indelError)
                        | (tumor.totalDepth < VariantCalling.min_cov_position)
                        | (tumor.FREQ < VariantCalling.min_allele_frequency)
                        | (tumor.ADF <= 0)
                        | (tumor.ADR <= 0)
                    )
                    tumor.drop(tumor[drop_condition].index, inplace=True)

                if varscan_data is None:
                    varscan_data = tumor
                else:
                    varscan_data = pd.concat((varscan_data, tumor), sort=True)

        if os.path.exists(self.strelka_results_dir):
            strelka_data_list = []
            for strelka_type in ("snvs", "indels"):
                passed_vcf_filename = os.path.join(
                    self.strelka_results_dir, "passed.somatic.%s.vcf" % strelka_type
                )
                for record in vcf.Reader(filename=passed_vcf_filename):
                    samples = {}
                    for sample in record.samples:
                        if sample.sample == "NORMAL":
                            samples["normal"] = sample
                        elif sample.sample == "TUMOR":
                            samples["tumor"] = sample

                    tumor_data = samples["tumor"].data
                    if strelka_type == "snvs":
                        coverage = (
                            tumor_data.AU[VariantCalling.strelka_tier]
                            + tumor_data.CU[VariantCalling.strelka_tier]
                            + tumor_data.GU[VariantCalling.strelka_tier]
                            + tumor_data.TU[VariantCalling.strelka_tier]
                        )
                    else:
                        if VariantCalling.strelka_tier == 0:
                            coverage = tumor_data.DP
                        else:
                            coverage = tumor_data.DP2

                    # This is how the Strelka manual whats the frequency to be
                    # calculated. Disclaimer: this does not make sense from a
                    # math point of view. If you are concerned about this
                    # method, go ask Illumina.
                    for alternative_base in record.ALT:
                        if strelka_type == "snvs":
                            reference_coverage = getattr(
                                tumor_data, str(record.REF) + "U"
                            )[VariantCalling.strelka_tier]
                            alternative_coverage = getattr(
                                tumor_data, str(alternative_base) + "U"
                            )[VariantCalling.strelka_tier]
                        else:
                            reference_coverage = tumor_data.TAR[
                                VariantCalling.strelka_tier
                            ]
                            alternative_coverage = tumor_data.TIR[
                                VariantCalling.strelka_tier
                            ]

                        total_coverage = reference_coverage + alternative_coverage
                        if total_coverage == 0:
                            continue

                        current_data = {
                            "key": "%s:%d-%d_%s_%s"
                            % (
                                record.CHROM,
                                record.POS,
                                record.POS + len(record.REF) - 1,
                                record.REF,
                                alternative_base,
                            ),
                            "DP": coverage,
                            "FREQ": alternative_coverage / total_coverage,
                        }
                        strelka_data_list.append(current_data)

            strelka_data = pd.DataFrame(
                data=strelka_data_list, columns=("key", "DP", "FREQ")
            )
            del strelka_data_list
        else:
            strelka_data = None

        self.variants = pd.DataFrame(columns=("key", "DP", "FREQ", "method"))

        if strelka_data is None:
            strelka_data = pd.DataFrame(columns=("key", "DP", "FREQ"))
        else:
            self.variants = pd.concat([self.variants, strelka_data], sort=True)

        if varscan_data is None:
            varscan_data = pd.DataFrame(columns=("key", "DP", "FREQ"))
        else:
            varscan_data = varscan_data[["key", "DP", "FREQ"]].copy()
            self.variants = pd.concat([self.variants, varscan_data], sort=True)

        if mutect_data is None:
            mutect_data = pd.DataFrame(columns=("key", "DP", "FREQ"))
        else:
            mutect_data = mutect_data.rename(
                index=str, columns={"tot_cov": "DP", "tumor_f": "FREQ"}
            )[["key", "DP", "FREQ"]].copy()
            self.variants = pd.concat([self.variants, mutect_data], sort=True)

        self.variants.drop_duplicates(("key",), inplace=True)
        methods = []

        keysets = {
            "mutect": set(mutect_data.key),
            "varscan": set(varscan_data.key),
            "strelka": set(strelka_data.key),
        }

        def set_methods(row: pd.Series) -> None:
            current_methods = []
            if row.key in keysets["mutect"]:
                current_methods.append("Mutect1.17")
            if row.key in keysets["varscan"]:
                current_methods.append("VarScan2")
            if row.key in keysets["strelka"]:
                current_methods.append("Strelka")

            methods.append(":".join(current_methods))

        self.variants.apply(set_methods, axis=1)
        self.variants.method = methods

        with open(self.annovar_file, "w") as fd:
            for key in self.variants.key:
                splitted = re.split(r"[:_-]", key)
                if len(splitted) != 5:
                    continue
                fd.write("%s\n" % "\t".join(splitted))

        self.variants.to_csv(self.variants_filename, index=False)

        self.analysis.logger.info("Finished data preparation for ANNOVAR")

    def annovar(self) -> None:
        """Run ANNOVAR.

        This function must be run after `prepare_for_annovar`.
        """
        self.analysis.logger.info("Running ANNOVAR")
        config = self.analysis.config

        os.chdir(config.annovar_basedir)
        executor = Executor(self.analysis)

        executor(
            f"{config.perl} table_annovar.pl "
            f"{{input_filename}} humandb/ "
            f"-buildver {self.build_version} "
            f"-protocol ensGene,avsnp151,cosmic100,clinvar_20240917,"
            f"gnomad41_genome,dbnsfp47a,dbnsfp47a_interpro,GTEx_v8_eQTL,GTEx_v8_sQTL  "
            f"-operation g,f,f,f,f,f,f,f,f -nastring NA -remove -v",
            input_filenames=[self.annovar_file],
            override_last_files=False,
            error_string="ANNOVAR exited with status {status}",
            exception_string="annovar error",
            only_human=True,
        )

        self.analysis.logger.info("Finished running ANNOVAR")

    def collect_annotated_variants(self) -> None:
        """Collect and evaluate the results from ANNOVAR.

        The results obtained from ANNOVAR are collected and curated
        databases about mutations information are used in order to
        obtain the most damaging and dangerous mutations. It is also
        checked whether the mutations could be druggable or not.

        The results are also stored in the database, if possible.
        """
        self.analysis.logger.info("Collecting annotated variants from ANNOVAR")

        barcoded_sample = BarcodedFilename.from_sample(self.analysis.sample)
        assert barcoded_sample
        kit = utils.get_kit_from_barcoded(self.analysis.config, barcoded_sample)
        assert kit

        annotation = pd.read_table(self.multianno_filename, on_bad_lines='warn')
        annotation.rename(
            index=str, columns={"avsnp151": "snp", "COSMIC100": "cosmic"}, inplace=True
        )
        annotation = annotation[annotation.Chr.str.match(r"chr(?:\d{1,2}|[xXyY])")]
        annotation.insert(
            0,
            "id",
            annotation.apply(
                lambda row: "%s:%d-%d_%s_%s"
                % (row.Chr, row.Start, row.End, row.Ref, row.Alt),
                axis=1,
            ),
        )

        cancer_genes = pd.read_hdf(VariantCalling.dataset_filename, "cancer_genes")
        panel_drug = pd.read_hdf(VariantCalling.dataset_filename, "panel_drug")
        gene_info = pd.read_hdf(VariantCalling.dataset_filename, "gene_info")
        gene_info.drop_duplicates(subset="symbol", inplace=True)

        selected_cancer_genes = cancer_genes[
            cancer_genes.cancer_site == kit.cancer_site
        ]

        annotation.reset_index(inplace=True, drop=True)
        gene_info.reset_index(inplace=True, drop=True)
        annotation["gene_type"] = annotation.merge(
            gene_info, left_on="Gene.ensGene", right_on="symbol", how="left"
        ).cancer_type.values
        annotation.loc[annotation.gene_type == "rst", "gene_type"] = float("nan")
        annotation["cancer_gene_site"] = float("nan")
        selected_symbols = annotation["Gene.ensGene"].isin(selected_cancer_genes.symbol)
        annotation.loc[selected_symbols, "cancer_gene_site"] = kit.cancer_site

        annotation.loc[
            annotation["Func.ensGene"] == "splicing", "ExonicFunc.ensGene"
        ] = "splicing"
        annotation.loc[
            annotation["Func.ensGene"] == "splicing", "AAChange.ensGene"
        ] = annotation[annotation["Func.ensGene"] == "splicing"]["GeneDetail.ensGene"]
        annotation.loc[
            annotation["Func.ensGene"] == "UTR3", "AAChange.ensGene"
        ] = annotation[annotation["Func.ensGene"] == "UTR3"]["GeneDetail.ensGene"]
        annotation.loc[
            annotation["Func.ensGene"] == "UTR5", "AAChange.ensGene"
        ] = annotation[annotation["Func.ensGene"] == "UTR5"]["GeneDetail.ensGene"]

        annotation["Gene.ensGene"] = annotation["Gene.ensGene"].map(
            lambda value: re.split(r"[^A-Za-z0-9]", value)[0]
        )

        annotation.loc[
            annotation.CADD_phred > VariantCalling.medium_damage, "damaging"
        ] = "Medium"
        annotation.loc[
            (annotation.CADD_phred > VariantCalling.high_damage)
            | (
                annotation["ExonicFunc.ensGene"].isin(
                    (
                        "frameshift substitution",
                        "nonframeshift substitution",
                        "splicing",
                    )
                )
            ),
            "damaging",
        ] = "High"

        hgnc = pd.read_hdf(VariantCalling.dataset_filename, "hgnc")
        hgnc.dropna(subset=("chromosome",), inplace=True)

        annotation.sort_values(["Chr", "Start", "End"], inplace=True)
        hgnc.sort_values(["chromosome", "tstart", "tend"], inplace=True)
        annotation.reset_index(drop=True, inplace=True)
        hgnc.reset_index(drop=True, inplace=True)

        annotation_ranges = GenomicRanges(
            [
                GenomicRange(row.Chr, row.Start - 1, row.End)
                for _, row in annotation.iterrows()
            ]
        )
        hgnc_ranges = GenomicRanges(
            [
                GenomicRange(row.chromosome, row.tstart - 1, row.tend)
                for _, row in hgnc.iterrows()
            ]
        )

        if annotation_ranges.resorted:
            raise Exception("annotation ranges expected to be not resorted")
        if hgnc_ranges.resorted:
            raise Exception("hgnc ranges expected to be not resorted")
        overlaps = annotation_ranges.overlaps(hgnc_ranges)
        annotation.loc[
            [index[0] for index in overlaps], "hgnc_refseq_accession"
        ] = hgnc.loc[[index[1] for index in overlaps]].refseq_accession.tolist()

        annotation["hgnc_canonical_refseq"] = annotation.apply(
            lambda row: "|".join(
                [
                    ensGene
                    for ensGene in str(row["AAChange.ensGene"]).split(",")
                    if re.search(str(row.hgnc_refseq_accession), ensGene)
                ]
            ),
            axis=1,
        )
        empty_canonical_refseqs = annotation.hgnc_canonical_refseq == ""
        annotation.loc[empty_canonical_refseqs, "alternative_refseq"] = annotation.loc[
            empty_canonical_refseqs, "AAChange.ensGene"
        ].tolist()
        annotation.loc[empty_canonical_refseqs, "hgnc_canonical_refseq"] = float("nan")

        if barcoded_sample.analyte == Analyte.GENE_PANEL:
            amplicons = pd.read_table(
                kit.amplicons,
                header=None,
                names=("chrom", "start", "end", "name", "length", "strand"),
                skiprows=2,
                delim_whitespace=True,
            )

            amplicons.sort_values(["chrom", "start", "end"], inplace=True)
            amplicons_ranges = GenomicRanges(
                [
                    GenomicRange(row.chrom, row.start, row.end)
                    for _, row in amplicons.iterrows()
                ]
            )

            amplicons_overlaps = annotation_ranges.overlaps(amplicons_ranges)
            annotation["in_gene_panel"] = False
            annotation.loc[
                set([index[0] for index in amplicons_overlaps]), "in_gene_panel"
            ] = True

        annotation["druggable"] = annotation["Gene.ensGene"].isin(
            panel_drug.gene_symbol
        )

        self.variants = pd.read_csv(self.variants_filename, index_col=False, on_bad_lines='warn')
        annotation.reset_index(inplace=True, drop=True)
        self.variants.reset_index(inplace=True, drop=True)

        annotation_all = annotation.merge(
            self.variants.rename(columns={"key": "id"}), on="id", how="inner"
        )
        annotation_all.to_csv(
            os.path.join(
                self.analysis.get_out_dir(), self.analysis.basename + "_variants.csv"
            ),
            index=False,
        )

        annotation_all[
            [
                "id",
                "Chr",
                "Start",
                "End",
                "Ref",
                "Alt",
                "Func.ensGene",
                "Gene.ensGene",
                "GeneDetail.ensGene",
                "ExonicFunc.ensGene",
                "AAChange.ensGene",
                "snp",
                "cosmic",
                "CLNALLELEID",
                "CLNDN",
                "CLNDISDB",
                "CLNREVSTAT",
                "CLNSIG",
                "ONCDN",
                "ONCDISDB",
                "ONCREVSTAT",
                "ONC",
                "SCIDN",
                "SCIDISDB",
                "SCIREVSTAT",
                "SCI",
                "gnomad41_genome_AF",
                "CADD_phred",
                "REVEL_score",
                "phyloP100way_vertebrate",
                "Interpro_domain",
                "GTEx_V8_eQTL_gene",
                "GTEx_V8_eQTL_tissue",
                "GTEx_V8_sQTL_gene",
                "GTEx_V8_sQTL_tissue",
                "gene_type",
                "damaging",
                "hgnc_refseq_accession",
                "hgnc_canonical_refseq",
                "alternative_refseq",
                "druggable",
                "DP",
                "FREQ",
                "method",
            ]
        ].to_csv(
            os.path.join(
                self.analysis.get_out_dir(),
                self.analysis.basename + "_variants_pretty.csv",
            ),
            index=False,
        )

        del annotation_all

        config = self.analysis.config
        if config.use_mongodb:
            from pymongo.errors import DocumentTooLarge

            self.variants = pd.read_csv(self.variants_filename, index_col=False, on_bad_lines='warn')
            variants = [
                {
                    key.replace(".", " "): value
                    for key, value in variant.items()
                    if type(value) != float or not math.isnan(value)
                }
                for variant in self.variants.to_dict("records")
            ]
            annotations = [
                {
                    key.replace(".", " "): value
                    for key, value in record.items()
                    if type(value) != float or not math.isnan(value)
                }
                for record in annotation.to_dict("records")
            ]
            del annotation
            for annotation in annotations:
                annotation.update({"assembly": self.build_version})

            db = Db(self.analysis.config)

            annotation_ids: List[str] = []
            for annotation in annotations:
                new_annotation = db.annotations.find_or_insert(
                    {"id": annotation["id"], "assembly": self.build_version}, annotation
                )
                assert new_annotation
                new_annotation_id = new_annotation["_id"]
                assert new_annotation_id
                annotation_ids.append(new_annotation_id)

            db_from_barcoded = db.from_barcoded(barcoded_sample)
            assert db_from_barcoded
            sequencing = db_from_barcoded["sequencing"]
            assert sequencing is not None

            try:
                db.analyses.find_or_insert(
                    {"sequencing": sequencing["_id"], "date": self.analysis.current},
                    {"variants": variants, "annotations": annotation_ids},
                )

            except DocumentTooLarge:
                self.analysis.logger.warning(
                    "annotations and variants cannot be saved inside the MongoDB, "
                    "document too large. Saving data using empty objects"
                )
                db.analyses.find_or_insert(
                    {
                        "sequencing": sequencing["_id"],
                        "date": self.analysis.current,
                        "assembly": self.build_version,
                    },
                    {"variants": [], "annotations": []},
                )

        self.analysis.logger.info("Finished collecting annotated variants from ANNOVAR")

    def run(self) -> None:
        """Run the variant calling algorithm."""
        if not self.analysis.run_fake and (
            len(self.mutect_filenames)
            + sum([len(values) for values in self.varscan_filenames.values()])
            > 0
        ):
            self.prepare_for_annovar()
            self.annovar()
            self.collect_annotated_variants()
