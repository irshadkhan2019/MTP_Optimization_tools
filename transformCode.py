import os
import libcst as cst
from difflib import HtmlDiff
import argparse
from pathlib import Path
from bulkUploadDetector import scan_file_for_bulk_ops


# Fields that we are replacing.
RELATED_FIELDS = {"ForeignKey", "OneToOneField"}

class AutoPrefetchTransformer(cst.CSTTransformer):
    def __init__(self, path: str):
        self.path = path 
        # This boolean variables determine whether to add import statement or not
        self.should_add_import = True
        # This boolean variables determine whether the file was modified ,used for showing diff.
        self.modified = False
        # This boolean variables determine whether the file has model class.
        self.is_model_class = False

    
    # Decide where the file is model file 
    def visit_ClassDef(self, node):

        # Check if file has base class name models.Model,if yes then this code is for db Models.
        for base in node.bases:
            if isinstance(base.value, cst.Attribute):
                if (
                    isinstance(base.value.value, cst.Name)
                    and base.value.value.value == "models"
                    and base.value.attr.value == "Model"
                ):
                    self.is_model_class = True
                    break

        # Check if any field like models.ForeignKey is present in class body.
        # Useful for Deriveclass -> Baseclass-> models.Model
        if not self.is_model_class:
            for stmt in node.body.body:
                if isinstance(stmt, cst.SimpleStatementLine):
                    for expr in stmt.body:
                        if isinstance(expr, cst.Assign):
                            value = expr.value
                            if isinstance(value, cst.Call) and isinstance(value.func, cst.Attribute):
                                if (
                                    isinstance(value.func.value, cst.Name)
                                    and value.func.value.value == "models"
                                    and value.func.attr.value in RELATED_FIELDS
                                ):
                                    self.is_model_class = True
                                    return True  
        return True  



    # Detect if auto_prefetch is already imported.
    def leave_Import(self, original_node, updated_node):
        for importAlias in updated_node.names:
            if importAlias.name.value == "auto_prefetch":
                self.should_add_import = False
        return updated_node

    # Change models.Model to autoprefetch.Model
    def leave_ClassDef(self, original_node, updated_node):
        # print(f"\n Visiting class: {original_node.name.value}")

        # Store all modified bases after process the file
        new_bases = []

        # Get the exact base class name and change it
        for base in updated_node.bases:
            try:
                base_code = cst.Module([]).code_for_node(base.value)
            except Exception as e:
                print(f" Skipping base Error: {e}")
                new_bases.append(base)
                continue
            
            # Change base when it matches model.Models eg. class BaseModel(models.Model) ,here base
            # models.Model will be changed to auto_prefetch.Model
            if base_code.strip() == "models.Model":
                # print("Matched models.Model")
                new_attr = cst.Attribute(
                    value=cst.Name("auto_prefetch"),
                    attr=cst.Name("Model")
                )
                base = base.with_changes(value=new_attr)
                self.modified = True

            new_bases.append(base)

        # Dont change Meta class if not Model file.
        if self.is_model_class:
            print("THis is model file",self.path)
            
        if not self.is_model_class:
            return updated_node.with_changes(bases=new_bases)

        meta_class_new_body = []
        # Check if class Meta is present if yes then add inheritance to it.
        for stmt in updated_node.body.body:
            if isinstance(stmt, cst.ClassDef) and stmt.name.value == "Meta":
                print("Changing Meta for file",self.path)
                if not stmt.bases:
                    new_base = cst.Arg(
                        value=cst.Attribute(
                            value=cst.Attribute(
                                value=cst.Name("auto_prefetch"),
                                attr=cst.Name("Model")
                            ),
                            attr=cst.Name("Meta")
                        )
                    )
                    stmt = stmt.with_changes(bases=[new_base])
                    self.modified = True
            meta_class_new_body.append(stmt)

        # Store the updated bases and set modified true since we changed/modified the file.
        if self.modified:
            return updated_node.with_changes(
                bases=new_bases,
                body=updated_node.body.with_changes(body=meta_class_new_body)
            )   

        return updated_node


    # Replace field definitions 
    def leave_Attribute(self, original_node, updated_node):
        # if we get fields with name present in RELATED_FIELDS then replace models with auto_prefetch.
        if (
            isinstance(original_node.value, cst.Name)
            and original_node.value.value == "models"
            and original_node.attr.value in RELATED_FIELDS
        ): 
            # print(f"\n Visiting attribue: {original_node.value.value,original_node.attr.value }")
            new_node = updated_node.with_changes(
                value=cst.Name("auto_prefetch")
            )
            # Here ,we have modified attribute so set modified to true for diff functionality.
            self.modified = True
            return new_node

        return updated_node

    # Based on boolean condition of should_add_import and modified we add import auto_prefetch 
    def leave_Module(self, original_node, updated_node):
        if self.modified and self.should_add_import:
            import_stmt = cst.SimpleStatementLine(
                [cst.Import(names=[cst.ImportAlias(name=cst.Name("auto_prefetch"))])]
            )
            return updated_node.with_changes(body=[import_stmt] + list(updated_node.body))
        return updated_node


def show_code_diff(original_code, modified_code, file_path):    
    # Store original and modified code
    original_lines = original_code.splitlines()
    modified_lines = modified_code.splitlines()

    # Create html object to get diff stored as html document
    d = HtmlDiff()

    # Pass code to get the diff with 2 colms name <file_path > original and modified.
    html_diff = d.make_file(
        original_lines,
        modified_lines,
        f"{os.path.basename(file_path)} (orignal)",
        f"{os.path.basename(file_path)} (modified)",
        ""
    )

    # Store the diff
    subdir = "diffs"
    # Create subdir if not exist
    os.makedirs(subdir, exist_ok=True)  

    # Create full output path
    output_filename = os.path.join(subdir, f"{os.path.basename(file_path)}.html")

    print(output_filename)
    with open(output_filename, "w", encoding="utf-8") as f:
        f.write(html_diff)

    print(f" Wrote diff  to {output_filename}")


def apply_autoprefetch_refactor(path):

    #Read the file 
    with open(path, "r") as f:
        source = f.read()

    # Parse and trnsform the code
    tree = cst.parse_module(source)
    transformer = AutoPrefetchTransformer(path)
    new_tree = tree.visit(transformer)

    new_code = new_tree.code

    # Check if modified then stored the diff
    if transformer.modified:
        # print(f"\Modified file path is: {path}")
        show_code_diff(source, new_code, path)

        # Write changes back
        with open(path, "w") as f:
            f.write(new_code)

def find_model_files(root_dir, skip_dirs=None):
    # Skip these folders since not useful.
    if skip_dirs is None:
        skip_dirs = {"node_modules","pynanolog","scripts","staticfiles","static","commands","myenv","migrations","management","__pycache__"}

    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]

        # Join dirpath with file name to give absolute path to refactor function.
        for filename in filenames:
            if filename.endswith('.py'):
                # print(file)
                yield os.path.join(dirpath, filename)

# Pass each file path to refactor function to change that file

if __name__ == "__main__":
    root_path = input("Enter the path to your Django project: ").strip()

    if not os.path.isdir(root_path):
        print(f"Invalid directory: {root_path}")
    else:
        print(f"\n🔍 Scanning {root_path} for Python files...\n")
        file_count = 0
        for file_path in find_model_files(root_path):
            file_count += 1
            apply_autoprefetch_refactor(file_path)
        print(f"\n Refactoring complete! Processed {file_count} files.\n")