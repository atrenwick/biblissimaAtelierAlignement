# --- Imports ---
# 1.1. Imports: Standard Library
import argparse
import contextlib
import glob
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from functools import partial
from concurrent.futures import ThreadPoolExecutor, as_completed

# 1.2. Third-Party Libraries
#from collatex import CollateX  # Avoid `import *`!
from collatex.core_classes import Collation
from collatex.core_functions import collate

from lxml import etree
from tqdm import tqdm

# 1.3. Local Application Imports
from text_matcher import matcher

# --- no Constants, no globals to declare ---

# --- Data Structures ---
#3 DataStruct and classes


@dataclass
class Paths:
    """
    Holds and manages the directory paths used by the pipeline.

    `log_dir` and `temp_dir` they default to subdirectories 
    ("logs" and "temp") nested inside `output_dir`.
    Directories are not created automatically on disk -- call
    `create_dirs()` to do that explicitly.

    Attributes:
        output_dir: The root directory where outputs are written.
        log_dir: Directory for log files. Defaults to `output_dir / "logs"`
            if not provided.
        temp_dir: Directory for temporary/intermediate files. Defaults to
            `output_dir / "temp"` if not provided.
    """
    output_dir: Path
    log_dir: Path = None
    temp_dir: Path = None

    def __post_init__(self):
        """
        Fills in default values for `log_dir` and `temp_dir` if they
        weren't explicitly supplied at construction time.

        Runs automatically immediately after the generated __init__,
        as part of the standard dataclass lifecycle.
        """
        if self.log_dir is None:
            self.log_dir = self.output_dir / "logs"
        if self.temp_dir is None:
            self.temp_dir = self.output_dir / "temp"    
    def create_dirs(self) -> None:
        """
        Explicitly creates all required directories on disk.
        """
        for path in (self.output_dir, self.log_dir, self.temp_dir):
            path.mkdir(parents=True, exist_ok=True)
    
# --- Context Managers / Utilities ---

@contextlib.contextmanager
def suppress_fd_stdout():
    """
    Context manager that silences stdout at the OS file-descriptor level.

    Unlike contextlib.redirect_stdout, which only swaps out Python's
    sys.stdout object, this suppresses ANY write to file descriptor 1
    (stdout) for the duration of the block -- including output from
    C extensions, subprocesses, or code that captured a reference to
    the original stdout stream before redirection. Use this when
    contextlib.redirect_stdout alone isn't catching everything.

    Yields:
        None
    """
    # Get the real OS-level file descriptor for stdout (usually 1)
    stdout_fd = sys.stdout.fileno()

    # Duplicate the current stdout fd so we can restore it later
    saved_fd = os.dup(stdout_fd)

    # Open a handle to the OS's "black hole" device
    devnull_fd = os.open(os.devnull, os.O_WRONLY)

    try:
        # Flush anything already buffered before we redirect, so it
        # doesn't get silently discarded or appear out of order later
        sys.stdout.flush()

        # Point the stdout fd at devnull -- from here on, all writes
        # to stdout (Python-level or lower) go nowhere
        os.dup2(devnull_fd, stdout_fd)

        yield  # caller's code runs here, with stdout suppressed

    finally:
        # Flush again in case the suppressed code buffered any output
        sys.stdout.flush()

        # Restore the original stdout fd so normal output resumes
        os.dup2(saved_fd, stdout_fd)

        # Clean up the duplicated file descriptors
        os.close(saved_fd)
        os.close(devnull_fd)
        
# ---  Functions ---

def run_all(file1, file2, paths, output_filename, n_procs, verbose=False, export_intermed=False, devtest=False, skip=0):
    """
    Executes the complete processing pipeline, from directory creation to matching 
    and thread alignment, or performs final consolidation if specified.

    Args:
        file1 (str): Path to the first XML file.
        file2 (str): Path to the second XML file.
        paths (Paths): An instance of the Paths dataclass used to manage directory paths and subfolders.
        output_filename (str): The filename for the final consolidated output.
        n_procs (int): Number of processor cores to utilize for thread alignment.
        verbose (bool, optional): If True, enables detailed logging. Defaults to False.
        export_intermed (bool, optional): If True, exports intermediate results to disk. Defaults to False.
        devtest (bool, optional): If True, runs the pipeline in development/test mode. Defaults to False.
        skip (int, optional): Control flag to skip pipeline stages. If set to 6, 
            the function bypasses matching and only runs the final consolidation. Defaults to 0.

    Returns:
        None
    """
    
    paths.create_dirs()
    if skip == 6:
        #if 6, concatenate only
        final_consolidator(paths, output_filename)
    else:
        results = run_matchers(file1, file2, paths)
        thread_align(results[0], results[1], n_procs=n_procs, paths=paths, output_filename=output_filename, devtest=False, futures=True)

def run_matchers(file1, file2, paths, verbose=False, export_intermed=False):
    """
    Orchestrates the first step of the processing pipeline by converting file1 and file2 
    to JSON and performing the initial matching between them.

    Args:
        file1 (str): Path to the XML file1 (used as the base reference).
        file2 (str): Path to the XML file2 (used as the target for matching).
        paths (Paths): An instance of the Paths dataclass used to manage directory paths and subfolders.
        verbose (bool, optional): If True, enables verbose message printing. Defaults to False.
        export_intermed (bool, optional): If True, exports intermediate matching results to disk. Defaults to False.

    Returns:
        List[etree.Element, etree.Element]: A list containing the processed XML trees 
            [f1_processed_tree, f2_processed_tree] resulting from the matching process.
    """
    print("Parsing XML files")

    files = [file1, file2]
    names = [os.path.basename(file).replace('.xml','') for file in files ]
    outputs = xml_to_json(files, names, paths)
    f1_json, f2_json = outputs[0], outputs[1]
        
    liste_f1Matcher = []
    liste_f1Matcher.append(f2_json)
    
    f1_processed_tree = etree.Element("xml")
    f2_processed_tree = etree.Element("xml")
    for x in liste_f1Matcher:
        matched_items = boucleMatch(f1_json,x, paths, verbose=False, export_intermed=True)
        f1_processed_tree.append(matched_items[0])
        f2_processed_tree.append(matched_items[1])

    return_list = [f1_processed_tree, f2_processed_tree]
    return return_list

def boucleMatch(texte1,texte2, paths, n_procs=2, verbose=False, export_intermed=False):
    '''
    find matches in texts
    args:
        text1: dict : text1 as dict
        text2: dict : text2 as dict
        temp_dir: str : absolute path to temporary dir where logs and intermediate files are written
        output_dir : str : absolute path to directory where output texts are to be written
    '''

    #get list of divs, and id, for each text
    liste1, idText1 = texte1["div"], texte1["idGnl"]
    liste2, idText2 = texte2["div"], texte2["idGnl"]

    v1_text_Element = etree.Element("text")
    v2_text_Element = etree.Element("text")

    print("Matching divs::::")
    for i1 in tqdm(liste1):
        if verbose:
            print(i1)
        
        c1 = i1["id"]
        corresp1 = i1["corresp"]
        position_info_dict = get_position_info_dict(i1)
        textToMatch1 = position_info_dict['token']
        elem1 = position_info_dict["element"]
        
        for i2 in liste2:
            if i2["corresp"]==c1:
                i2_position_info_dict = get_position_info_dict(i2)
                textToMatch2 = i2_position_info_dict["token"]
                elem2 = i2_position_info_dict["element"]
                c2 = i2["id"]
                corresp2 = c1
    
        ta = matcher.Text(textToMatch1, c1)
        tb = matcher.Text(textToMatch2, c2)
        
        ## note on verbosity : if verbose is set, `Extending forwards/backwards` will be printed as this happens on call to the method and thus prints output
        ## the call_quietly function  can intercept this when these calls are triggered inside the lambda function, but not before
        if verbose:
            obj = matcher.Matcher(ta, tb, ngramSize=4)
            m = call_quietly(obj.match)
        else: 
            m = call_quietly(lambda: matcher.Matcher(ta, tb, ngramSize=4).match())

        pos1, pos2 = m[1], m[2]
        l1, l2 = recupid(c1,pos1,elem1), recupid(c2,pos2,elem2)

        m_ecr, l1_ecr, l2_ecr = str(m), str(l1), str(l2)
        if verbose:
            print(len(l1["valeurs"]))
            print(len(l2["valeurs"]))

        if export_intermed:
            logfile_fullpath = f'{paths.log_dir}/log_match{c1}_{c2}.txt'
            chunks = ["IDs of the matching words in the two textx:", l1_ecr, l2_ecr, "Text_matcher log::", m_ecr]

            with open(logfile_fullpath, "w") as text_file:
               for chunk in chunks:
                   _ = text_file.write(chunk)

        xml1, xml2 = prodXML(elem1,l1,idText1,c1,corresp1, paths.temp_dir), prodXML(elem2,l2,idText2,c2,corresp2, paths.temp_dir)
        v1_text_Element.append(xml1)
        v2_text_Element.append(xml2)

    textIDs = [f'{idText1}', f'{idText2}']
    text_elements = [v1_text_Element, v2_text_Element]
    
    if export_intermed:
        textfiles = [f'{paths.temp_dir}/export_par_div_{ID}test2.xml' for ID in textIDs]
        for textfile, textID, text_element in zip(textfiles, textIDs, text_elements):
            print_tree = etree.ElementTree(text_element)
            print_tree.write(textfile, encoding='UTF-8', pretty_print=True)


    ## make payloads to pass to exporter function
    payloads = list(zip(text_elements, textIDs))
    worker_func = partial(exportDef_py_worker, output_dir=paths.output_dir, export_intermed=export_intermed)
    
    # 3. Execute with ThreadPoolExecutor
    # Pre-allocate list to maintain order
    return_items = [None] * len(payloads)  
    
    with ThreadPoolExecutor(max_workers=n_procs) as executor:
        # Map each future to its original index to preserve order later
        future_to_index = {executor.submit(worker_func, p): i for i, p in enumerate(payloads)}

        for future in tqdm(as_completed(future_to_index), total=len(payloads), desc="Exporting to xml"):
            index = future_to_index[future]
            try:
                return_items[index] = future.result()
            except Exception as e:
                print(f"Error processing item {index}: {e}")
    print("Preparing to match at token level…")            
    return return_items

def thread_align(A1, B1, n_procs, paths, output_filename, devtest=False, futures=False):
    """
    Aligns blocks of text using a thread pool executor.

    Args:
        blocks (list of lxml.etree._Element elements): A list of blocks to be processed by the align_block function.
        A1 (lxml.etree._ElementTree): The first alignment reference/object passed to the worker.
        B1 (lxml.etree._ElementTree): The second alignment reference/object passed to the worker.
        n_procs (int): The number of worker threads to use in the ThreadPoolExecutor.
        output_dir (str): The directory path where output files should be saved.
        devtest (bool, optional): If True, enables development/test mode in the worker. 
            Defaults to False.
        futures (bool, optional): If True, uses `executor.submit` and `as_completed`, 
            processing results as they finish to update tqdm pbar. If False, uses `executor.map`, 
            yielding results in the order they were submitted. Defaults to False.

    Returns:
        None
    """

    if devtest == True:
        print("*************  Dev mode activated, skipping file writes   *************")
        A1 = etree.parse(file1) 
        B1 = etree.parse(file2)
        print("Trees loaded")
        
    blocks = A1.xpath("//text//div")
    print(f'Token matching for {len(blocks)} divs with {n_procs} workers' )
    
    worker = partial(align_block_wrapper, A1=A1, B1=B1, paths=paths, devtest=devtest)
    with ThreadPoolExecutor(max_workers=n_procs) as executor:
            with ThreadPoolExecutor(max_workers=n_procs) as executor:
                futures = [executor.submit(worker, item) for item in enumerate(blocks, start=1)]
                for future in tqdm(as_completed(futures), total=len(blocks)):
                    future.result()

    if devtest == False:    
        final_consolidator(paths, output_filename)

def align_block(div, propreId, A1, B1, paths, devtest=False):
    """
    Performs a paragraph-level collation between two XML documents for a specific block ie a div.

    The function identifies a corresponding division in source B1 based on the ID of 
    the provided division from source A1. It then iterates through the paragraphs 
    within those divisions, matching them by their index 'n', performs a collation 
    comparison, and exports the results to a new XML file.

    Args:
        div (lxml.etree._Element): The current XML division element being processed from source A1.
        propreId (int/str): A unique identifier for the current block, used for naming the output file.
        A1 (lxml.etree._ElementTree): The parsed XML tree of the first source document.
        B1 (lxml.etree._ElementTree): The parsed XML tree of the second source document.
        output_dir (str): The directory path where the resulting collation XML files will be saved.
        devtest (bool, optional): If True, the function will skip writing the final XML file 
            to disk. Defaults to False.

    Process:
        1. Extracts the XML namespace ID from the input `div`.
        2. Locates the corresponding division in `B1` using an XPath query on the `corresp` attribute.
        3. Creates a new XML output element `<div id="divColl[propreId]" corresp="[idA]_[idB]">`.
        4. For every paragraph `<p>` in the `A1` division:
            a. Extracts the paragraph index `n`.
            b. Finds the matching paragraph in the `B1` division using the same index `n`.
            c. Converts both paragraphs to a JSON-compatible format via `xml_to_collatex_json`.
            d. Adds these witnesses to a `Collation` object.
            e. Generates a collation table and converts it back to an XML element via `notre_export_xml`.
            f. Appends the resulting paragraph to the output division.
        5. Saves the final constructed XML tree to `{output_dir}/collationParDiv[propreId].xml`, each
            thread exporting the XML as soon as complete. XMLs are later read-in then collated.
            
    Returns:
        None
    """
    iDiv = div.get('{http://www.w3.org/XML/1998/namespace}id')
    for divEz in B1.xpath(f"//text//div[@corresp='{iDiv}']"):
        iDivEz = divEz.get('{http://www.w3.org/XML/1998/namespace}id')

    outputDivElement = etree.Element("div")
    outputDivElement.attrib["{http://www.w3.org/XML/1998/namespace}id"] = f'divColl{str(propreId)}'
    outputDivElement.attrib["corresp"]  = f"{iDiv}_{iDivEz}"
    
    target_ps = A1.xpath(f"//text//div[@xml:id='{iDiv}']/p")

    for i, par in (enumerate(target_ps, start=1)):
        i = par.get("n") 
        A = par
        json_input = {}
        json_input['witnesses'] = []
        json_input['witnesses'].append(xml_to_collatex_json('A',A))

        b_blocks = B1.xpath(f"//text//div[@xml:id='{iDivEz}']/p[@n='{i}']")
        for p in b_blocks:
            if len(b_blocks) >0:
                B = p
                json_input['witnesses'].append(xml_to_collatex_json('B',B))
            else:
                print(f"No b blocks for iDiv == {iDiv} == {iDivEz}")

        json_collation = Collation()
        for witness in json_input["witnesses"]:
            json_collation.add_witness(witness)

        table = collate(json_collation, output="table",segmentation=False)
        p_element = notre_export_xml(table, i)
        outputDivElement.append(p_element)

    valDiv_output_fullpath = f'{paths.temp_dir}/collationParDiv{str(propreId).zfill(2)}.xml'
    if devtest == False:    
        export_tree = etree.ElementTree(outputDivElement)
        export_tree.write(valDiv_output_fullpath, encoding='UTF-8', pretty_print=True)

def align_block_wrapper(indexed_block, A1, B1, paths, devtest=False):
    """
    A wrapper function that unpacks an indexed block and passes it to the align_block function.

    This function is primarily used as a helper to handle blocks that have been 
    enumerated (indexed), ensuring the ID and the block content are passed 
    as separate arguments to the core alignment logic.

    Args:
        indexed_block (tuple): A tuple containing (propreId, block), where 
            propreId is the unique identifier and block is the content to be aligned.
        A1 (lxml.etree._Element): An XML tree element parsed from the first source.
        B1 (lxml.etree._Element): An XML tree element parsed from the second source.
        output_dir (str): The directory path where alignment results should be saved.
        devtest (bool, optional): If True, enables development or testing mode 
            within the align_block function. Defaults to False.

    Returns:
        The return value of the align_block function.
    """
    propreId, block = indexed_block
    return align_block(block, propreId=propreId, A1=A1, B1=B1, paths=paths, devtest=devtest)

def call_quietly(func, *args, **kwargs):
    """
    Call `func` with the given arguments while suppressing all stdout output.

    Args:
        func: The callable to invoke.
        *args: Positional arguments to pass to `func`.
        **kwargs: Keyword arguments to pass to `func`.

    Returns:
        Whatever `func(*args, **kwargs)` returns.
    """
    with suppress_fd_stdout():
        return func(*args, **kwargs)
                    
def get_position_info_dict(witness):
    """
    Processes witness tokens to generate a reconstructed text string and 
    calculate the character start positions for each token.

    Args:
        witness (dict): The witness dictionary containing the 'tokens' list.

    Returns:
        dict: A dictionary with the reconstructed string ('token') and the 
              enriched token list ('element').
    """
    token_witness = witness["tokens"]
    pos = 0
    liste_de_tok_wit = [] 

    ## iterate over tokens
    for i in range (0, len(token_witness)) :
        ## indexing logic
        token_witness[i]["n"]=i 
        elem = token_witness[i]

        ## shared logic getting lemma if present, otherwise getting text
        if elem['lemme'] != '': 
            l = elem['lemme'] 
        else:
            l = elem['text'] 
        
        ## build string list as final step of processing for `token` output 
        liste_de_tok_wit.append(l) ## tok

        ## position-length logic for `element` output
        # for first word, start at 0, then increment position by length of the form
        if elem["n"]==0:
            elem["debut_mot"] = pos
            pos = len(l)
        # for other tokens, increment by 1 for space, add k:v to dict then increment by length of the form
        else:
            pos += 1
            elem["debut_mot"] = pos
            pos += len(l)

    ## turn list of tokens into 1 string
    text = " ".join(liste_de_tok_wit) 
    ## make a dict to return
    info_dict = {"token": text, "element": token_witness}
    return info_dict

def xml_div_to_json(name, raw_tree, temp_dir, level="div", verbose=False):
    """
    Extracts specific structural elements (defined by 'level') from a TEI XML tree 
    and converts them into a JSON-compatible dictionary.

    The function searches for elements of the specified level that possess an 
    'xml:id' and are descendants of the <text> element. Each matching element 
    is processed via `xml_to_tei_json` and added to a witness dictionary. 
    The final dictionary is then saved to the temporary directory in two formats: 
    a raw string representation and a formatted JSON file.

    Args:
        name (str): The unique identifier for the witness, used for the 
            dictionary ID and output filenames.
        raw_tree (etree._ElementTree): The parsed XML tree to be processed.
        temp_dir (str): The path to the temporary directory where the 
            resulting JSON files will be saved.
        level (str, optional): The XML tag name to target (e.g., "div"). 
            Defaults to "div".
        verbose (bool, optional): If True, prints the elements and IDs 
            during the parsing process. Defaults to False.

    Returns:
        dict: The witness dictionary containing the identifier ('idGnl') 
            and a list of processed divisions ('div').
    """

    #create dict,add metas
    witness = {
        'idGnl': name,
        'div': []
    }

    liste_par = witness['div']
    ns_decl = {'tei': 'http://www.tei-c.org/ns/1.0'}
    
    for par in raw_tree.xpath(f"descendant::tei:{level}[@xml:id and ancestor::tei:text]", namespaces=ns_decl):
        if verbose:
            print(par)
        iden = par.get("{http://www.w3.org/XML/1998/namespace}id")
        corresp = par.get("corresp")
        if verbose:
            print(f'iden == {iden} :: par_type == {type(par)} :: par = {par}')

        #get tei_json, append to list
        div = xml_to_tei_json(iden,corresp,par)
        liste_par.append(div)
    
    output_filename_full = os.path.join(temp_dir, f'{name}_dico_orig.json')
    with open(output_filename_full, "w") as text_file:
        _ = text_file.write(str(witness))

    output_filename_full2 = os.path.join(temp_dir, f'{name}_dico_mod.json')
    with open(output_filename_full2, "w", encoding="UTF-8") as text_file:
        json.dump(witness, text_file, ensure_ascii=False, indent=4)

    return witness

def xml_to_tei_json(iden,corresp,xmlInput):
    """
    Transforms a TEI XML element into a structured dictionary containing metadata 
    and a list of tokenized words.

    The function uses an XSLT transformation to extract specific attributes from 
    `<tei:w>` elements, including the text, XML ID, lemma, part-of-speech (pos), 
    and morphological features (msd). It includes a specific cleaning step to 
    remove '+' characters from lemmas to ensure compatibility with downstream 
    tokenizers.

    Args:
        iden (str): The unique identifier for the division/element.
        corresp (str): The correspondence value associated with the element.
        xmlInput (etree._Element): The TEI XML element to be transformed.

    Returns:
        dict: A dictionary containing:
            - 'id': The provided identifier.
            - 'corresp': The provided correspondence value.
            - 'tokens': A list of dictionaries, where each dictionary represents 
              a token with keys 'text', 'id', 'lemme' (cleaned), 'pdd' (pos), and 'msd'.
    """

    div = {
      "id" : iden,
      "corresp": corresp
      }

    #define XSLT to get dicts from each div
    xml_to_tei_json_xslt = etree.XML('''
<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema"
    xmlns:tei="http://www.tei-c.org/ns/1.0"
    exclude-result-prefixes="tei xs"
    version="1.0">
    <xsl:output method="text"/>

    <xsl:template match="/">
        <xsl:text>[</xsl:text>
        <xsl:apply-templates/>
        <xsl:text>]</xsl:text>
    </xsl:template>
    
        <xsl:template match="tei:teiHeader"/>
        
        <xsl:template match="tei:div|tei:ab|tei:lg|tei:p|tei:l">
        <xsl:apply-templates/>
        </xsl:template>
        
    <xsl:template match="tei:head"/>
    
    <xsl:template match="tei:w">
        <xsl:text>{"text": "</xsl:text>
        <xsl:apply-templates/>
        <xsl:text>", "id": "</xsl:text>
        <xsl:value-of select="@xml:id"/>
        <xsl:text>", "lemme": "</xsl:text>
        <!--pour la valeur des lemmes, on enlève les + qui gênent sinon le calcul du nb de caractères à cause du tokeniser de text_matcher-->
        <xsl:choose>
            <xsl:when test="contains(@lemma, '+')">
                <xsl:value-of select="concat(substring-before(@lemma, '+'), substring-after(@lemma, '+'))"/>
            </xsl:when>
            <!-- s'il y a une valeur de lemme -->
            <xsl:otherwise>
                <xsl:value-of select="@lemma"/>
            </xsl:otherwise>
        </xsl:choose>
        <xsl:text>", "pdd":"</xsl:text>
        <xsl:value-of select="@pos"/>

        <xsl:text>", "msd":"</xsl:text>
        <xsl:value-of select="@msd"/>

        <xsl:text>"}</xsl:text>
        <xsl:if test="following::tei:w">
            <xsl:text>, </xsl:text>
        </xsl:if>
    </xsl:template>
</xsl:stylesheet>
    ''')
    
    #parse the XSLT
    xml_to_tei_json_transformer = etree.XSLT(xml_to_tei_json_xslt)    
    # apply the transformation and parse output as json
    tei_json_string = str(xml_to_tei_json_transformer(xmlInput))
    i = json.loads(tei_json_string)
    # assign the output to the div dictionary for the tokens
    div["tokens"]=i
    return div

def recupid(iden,text,json):
    """
    Maps match positions back to their corresponding token identifiers.

    This function iterates through a list of match results and searches for the 
    token ID associated with each match's starting position (debut_mot) by 
    cross-referencing a list of token dictionaries.

    Args:
        iden (str/int): The unique identifier for the text or witness.
        text (List[Tuple/List]): A list of match results, where the first element 
            of each entry represents the starting character position of the match.
        json (List[dict]): A list of token dictionaries, where each dictionary 
            must contain 'debut_mot' (the starting position) and 'id' (the token identifier).

    Returns:
        dict: A dictionary containing:
            - 'id': The provided identifier for the text.
            - 'valeurs': A list of token IDs that correspond to the starting 
              positions found in the match results.
    """
    val = {}
    val["id"] = iden
    liste_des_val = []
    for i in range (0, len(text)) :  
        lieuText = text[i][0]       
        for elem in json:
            if lieuText ==  elem["debut_mot"]:
                liste_des_val.append(elem["id"])
    
    val["valeurs"] = liste_des_val
    return val

def exportDef_py(text_element):
    """
    Simplifies and reconstructs an XML text element by extracting specific structural 
    nodes and word tokens into a new, cleaned XML hierarchy.

    The function filters the input element for specific tags (w, p, div, lg, l, ab) 
    and reorganizes them. It ensures that word tokens ('w') are nested within 
    structural elements (like 'p'), while preserving critical attributes such as 
    'xml:id' and 'n'.

    Args:
        text_element (etree._Element): The source XML element to be processed and simplified.

    Returns:
        etree._Element: A new XML root element (<text>) containing the restructured 
            and filtered hierarchy of the original document.
    """
    
    input_text_element = text_element
    new_root = etree.Element("text")

    current_paragraph = None
    for element in input_text_element.xpath(f"descendant::node()[self::w or self::p or self::div or self::lg or self::l or self::ab]"):
        #if element.tag == "div" or element.tag == "lg": 
        if element.xpath(f"descendant::node()[self::p or self::div or self::lg or self::l or self::ab] and @xml:id"):
            nomElem = element.tag
            current_paragraph = None
            new_div = etree.SubElement(new_root, nomElem)
            new_div.attrib.update(element.attrib)
        elif element.tag == "w":
            # add word to para or start new, if there is actually a word
            if current_paragraph is None:
                current_paragraph = etree.SubElement(new_div, "p", n="0")
            new_word = etree.SubElement(current_paragraph, "w")
            new_word.attrib.update(element.attrib)
            new_word.text = element.text
        #elif element.tag == "p":
        elif element.xpath(f"ancestor::node()[self::div or self::lg or self::ab] and @n"):
            nomElemNivBas = element.tag
            current_paragraph = etree.SubElement(new_div, nomElemNivBas, n=element.attrib.get("n"))

    return new_root

def exportDef_py_worker(payload, output_dir, export_intermed=False):
    """
    Helper worker to handle the heavy lifting of exportDef_py 
    and the optional intermediate file export.
    """
    text_element, textID = payload
    
    # The heavy lifting
    def_item = exportDef_py(text_element)
    
    # Optional intermediate export
    if export_intermed:
        export_tree = etree.ElementTree(def_item)
        fullpath = f'{output_dir}/{textID}test2.xml'    
        export_tree.write(fullpath, encoding='UTF-8', pretty_print=True)
        
    return def_item

def prodXML(val,listev,cle,clediv,corresp, paths, export_intermed=False):
    """
    Reconstructs an XML division (div) by populating it with tokens and marking 
    matched tokens with paragraph (<p>) elements.

    This function iterates through a list of token data and builds an XML structure. 
    If a token's ID exists within the provided matched values list (`listev`), 
    a paragraph element is created to mark its position. All tokens are 
    converted into word elements (<w>) with their corresponding attributes 
    (lemma, pos, msd).

    Args:
        val (List[dict]): A list of token dictionaries containing 'lemme', 'id', 
            'text', 'n', 'pdd', and 'msd'.
        listev (dict): A dictionary containing the key 'valeurs', which is a 
            list of token IDs that were successfully matched.
        cle (Any): A general identifier (currently unused in the function body).
        clediv (str): The unique identifier to be assigned to the root <div> element.
        corresp (str): The correspondence value to be assigned to the root <div> element.
        paths (Paths): An instance of the Paths dataclass used to manage 
            the temporary directory for exports.
        export_intermed (bool, optional): If True, saves the resulting XML 
            structure to a file in the temporary directory. Defaults to False.

    Returns:
        etree._Element: The constructed XML <div> element containing the 
            restructured tokens and paragraphs.
    """

    liste_des_valeurs = listev["valeurs"]
    div = etree.Element("div")
    div.set("{http://www.w3.org/XML/1998/namespace}id", clediv)
    div.set("corresp", corresp)
    for i in val:
        l = i["lemme"]
        iden = i["id"]
        text = i["text"]
        pos = i["n"]
        pdd = i["pdd"]
        msd = i["msd"]
        if iden in liste_des_valeurs:
            piden = liste_des_valeurs.index(iden)+1
            piden = str(piden)
            p = etree.SubElement(div, "p")
            p.set("n", piden)

        e = etree.SubElement(div, "w")
        e.set("{http://www.w3.org/XML/1998/namespace}id", iden)
        if l != '':
            e.set("lemma", l)
        else:
            pass
        if pdd != '':
            e.set("pos", pdd)
        else:
            pass
        if msd != '':
            e.set('msd', msd)
        else:
            pass
        e.text = text
    if export_intermed:
        div_as_string = etree.tostring(div, encoding="unicode")
        clevid_outputpath = f'{paths.temp_dir}/export{clediv}.xml'
        with open(clevid_outputpath, 'w', encoding='UTF-8') as fichier:
            _ = fichier.write(div_as_string)

    return div

def xml_to_collatex_json(id,xmlInput):
    """
    Converts a tokenized and annotated XML input into a JSON format specifically structured for collation.

    This function applies an XSLT transformation to the provided XML input to extract 
    word tokens (elements marked as 'w') and their associated attributes (XML ID, 
    lemma, and part-of-speech), returning them as a structured dictionary.

    Args:
        id (str/int): The unique identifier for the witness being processed.
        xmlInput (etree._Element): The parsed XML tree containing the tokenized text.

    Returns:
        dict: A dictionary containing:
            - 'id': The provided witness identifier.
            - 'tokens': A list of dictionaries, where each dictionary represents a token 
              with keys 'text', 'i' (XML ID), 't' (lemma), and 'pos' (part-of-speech).
    """

    witness = {}
    witness['id'] = id
    transformer_raw = etree.XML('''
    <xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema"
    xmlns:tei="http://www.tei-c.org/ns/1.0"
    exclude-result-prefixes="xs tei"
    version="1.0">
    <xsl:output method="text"/>

    <xsl:template match="/">
        <xsl:text>[</xsl:text>
        <xsl:apply-templates/>
        <xsl:text>]</xsl:text>
    </xsl:template>
    
    <xsl:template match="w">
        <xsl:text>{"text": "</xsl:text>
        <xsl:apply-templates/>
        <xsl:text>", "i": "</xsl:text>
        <xsl:value-of select="@xml:id"/>
        <xsl:text>", "t": "</xsl:text>
        <xsl:value-of select="@lemma"/>
        <xsl:text>", "pos": "</xsl:text>
        <xsl:value-of select="@pos"/>
        <xsl:text>"}</xsl:text>
        <xsl:if test="following::w">
            <xsl:text>, </xsl:text>
        </xsl:if>
    </xsl:template>
</xsl:stylesheet>
    ''')
    xslt_transformer = etree.XSLT(transformer_raw)
    witness['tokens'] = json.loads( str(xslt_transformer(xmlInput)))
    return witness

def notre_export_xml(table, i):
    """
    Transforms a CollateX table result into a TEI-formatted XML paragraph element.

    This function takes a collation table (where columns represent aligned tokens 
    across witnesses) and converts it into an XML structure consisting of a 
    paragraph containing apparatus elements (<app>) and readings (<rdg>).

    Args:
        table (collatex.table.CollateXTable): A CollateX table object containing 
            the aligned tokens and witness data.
        i (int/str): The paragraph index number to be assigned to the 'n' attribute 
            of the resulting <p> element.

    Returns:
        lxml.etree._Element: An XML element <p> containing the nested apparatus 
            and reading elements based on the collation table.

    XML Structure Produced:
        <p n="i">
            <app>
                <rdg wit="#WitnessID" xml:id="tokenID" lemma="lemma" pos="pos">Text</rdg>
                ...
            </app>
            ...
        </p>

    Notes:
        - The function uses the XML namespace `http://www.w3.org/XML/1998/namespace` 
          for the 'id' attribute to ensure TEI compliance.
        - The readings within each apparatus are sorted by the witness key.
    """
    p_element = etree.Element("p", attrib={"n": str(i)})

    readings = []
    for column in table.columns:
        app = etree.SubElement(p_element, 'app')
        for key, value in sorted(column.tokens_per_witness.items()):
            child = etree.SubElement(app, 'rdg')
            child.attrib['wit'] = "#" + key

            for item in value:
                child.attrib["{http://www.w3.org/XML/1998/namespace}id"] = item.token_data["i"]
                child.attrib['lemma']= item.token_data["t"]
                child.attrib['pos'] = item.token_data["pos"]

            child.text = "".join(str(item.token_data["text"]) for item in value)

    return p_element

def xml_to_json(files, names, paths, verbose=False):
    """
    Converts a list of XML files into JSON format using division-based parsing logic.

    This function iterates through pairs of files and names, parses the XML content,
    and utilizes the `xml_div_to_json` helper function to transform the XML tree 
    into a JSON-compatible structure.

    Args:
        files (List[str]): A list of paths to the XML files to be processed.
        names (List[str]): A list of identifiers or filenames corresponding to each 
            XML file, used for labeling the output.
        paths (Paths): An instance of the Paths dataclass used to manage directory 
            paths and subfolders.

    Returns:
        List[dict]: A list of dictionaries containing the processed JSON outputs 
            generated from each XML file.

    Raises:
        OSError: If a file in `files` cannot be found or opened.
        etree.XMLSyntaxError: If an XML file is malformed.
    """
    outputs = []
    for file, name in tqdm(zip(files, names)):
      raw_tree = etree.parse(file)  
      json_output = xml_div_to_json(name, raw_tree, paths.temp_dir,  "div", verbose)
      outputs.append(json_output)
    return outputs

def final_consolidator(paths, output_filename):
    """
    Aggregates all XML files from a source directory into a single combined XML document.

    The function performs the following steps:
    1. Identifies and sorts all files with the '.xml' extension in the target directory.
    2. Initializes a new XML root element named <text>.
    3. Iterates through each discovered XML file, parses its content, and appends 
       the root element of that file as a child of the main <text> element.
    4. Writes the resulting combined XML tree to a file named 'combined.xml' 
       with UTF-8 encoding and pretty-printed formatting.

    Note:
        Currently uses hard-coded file paths for input and output. 
        A progress bar is displayed during the merging process via tqdm.

    Returns:
        None
    """
    output_filename_full = f'{paths.output_dir}/{output_filename}.xml'
    input_files = sorted(glob.glob(f'{paths.temp_dir}/collat*.xml'))
    if len(input_files) ==0:
        print(f"Error : no files found in {paths.temp_dir} with the prefix « collat » and the extension «.xml» ")
    else:
        print(f"Consolidating {len(input_files)} files :::: ")
        output_tree_element = etree.Element("text")
        for f in tqdm(input_files):
            current_input = etree.parse(f)
            output_tree_element.append(current_input.getroot())
    
    output_tree =etree.ElementTree(output_tree_element)
    output_tree.write(output_filename_full, encoding='UTF-8', pretty_print=True)
    print(f"Complete :: aligned file exported to {output_filename_full}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='''Align xml files.
    
    Usage : 
      python3 /scripts/run_aligner.py -file1 /data/textes/A.xml -file2 /data/textes/B.xml -output_dir /data/output/test7 -nprocs 4
            
    ''')
    parser.add_argument(
        "-file1", type=str,help="path to file1 AKA fileA"
    )
    parser.add_argument(
        "-file2", type=str,help="path to file2 AKA fileB"
    )
    parser.add_argument(
        "-output_dir", type=Path, required=True, 
        help="output directory, in which results and subfolders will be placed"
    )
    parser.add_argument(
        "-output_filename", type=str, default="combined",
        help="Name of file to be exported"
    )
    parser.add_argument(
        "-nprocs", type=int, default=2,
        help="Number of worker processes to use for token-level alignment, 1 div per worker"
    )
    parser.add_argument(
        "-verbose", type=bool, default=False,
        help="enable verbose to include print statements in console"
    )
    parser.add_argument(
        "-export_intermed", type=bool, default=False,
        help="export files at all intermediate steps"
    )
    parser.add_argument(
        "-devtest", type=bool, default=False,
        help="toggle for devtestingmode: no files created"
    )
    parser.add_argument(
        "-check", type=bool, default=False,
        help="check for errors"
    )
    parser.add_argument(
        "-skip", type=int, default=0,
        help="Skip steps ; 6 will run only consolidator"
    )
    args = parser.parse_args()
    
    file1 = args.file1
    file2 = args.file2
    paths = Paths(output_dir = args.output_dir)
    output_filename = args.output_filename
    n_procs = args.nprocs
    verbose = args.verbose
    export_intermed = args.export_intermed
    devtest = args.devtest
    check = args.check
    skip=args.skip
    if check:
       print("No catastrophic errors found")
    else:
        run_all(file1, file2, paths, output_filename, n_procs, verbose=verbose, export_intermed=export_intermed, devtest=devtest, skip=skip)

