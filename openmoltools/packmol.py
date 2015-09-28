import numpy as np
import shutil
import os
import mdtraj as md
from mdtraj.utils import enter_temp_directory
from mdtraj.utils.delay_import import import_
import tempfile
from distutils.spawn import find_executable
import simtk.unit as units

PACKMOL_PATH = find_executable("packmol")

HEADER_TEMPLATE = """
# Mixture 

tolerance %f
filetype pdb
output %s
add_amber_ter
seed %i

"""

BOX_TEMPLATE = """
structure %s
  number %d 
  inside box 0. 0. 0. %f %f %f
end structure
"""

SPHERE_TEMPLATE = """
structure %s
  number %d 
  inside sphere 0. 0. 0. %f
end structure
"""

FIXED_TEMPLATE = """
structure %s
  number %d 
  center
  fixed %d %d %d 0. 0. 0.
end structure
"""

def pack_box(pdb_filenames_or_trajectories, n_molecules_list, tolerance=2.0, shape='box', size=None, seed=1, fix=False):
    """Run packmol to generate a box containing a mixture of molecules.

    Parameters
    ----------
    pdb_filenames_or_trajectories : list({str, Trajectory})
        List of pdb filenames or trajectories for each component of mixture.  If this is
        a list of trajectories, the trajectories will be saved to as
        temporary files to be run in packmol.
    n_molecules_list : list(int)
        The number of molecules of each mixture component.
    tolerance : float, optional, default=2.0
        The mininum spacing between molecules during packing.  In ANGSTROMS!
    size : float, optional
        The size of the box/sphere to generate. In ANGSTROMS.
        For a box specifies the lengths of the box
        For a sphere specifies the diameter of the sphere
        Default generates boxes/spheres that are very large for increased stability.
        May require extra time for energy minimization and equilibration.

    Returns
    -------
    trj : MDTraj.Trajectory
        Single frame trajectory with mixture box.

    Notes
    -----
    Be aware that MDTraj uses nanometers internally, but packmol uses angstrom
    units.  The present function takes `tolerance` and `box_size` in 
    angstrom units, but the output trajectory will have data in nm.  
    Also note that OpenMM is pretty picky about the format of unit cell input, 
    so use the example in tests/test_packmol.py to ensure that you do the right thing.
    """
    assert len(pdb_filenames_or_trajectories) == len(n_molecules_list), "Must input same number of pdb filenames as num molecules"
    
    pdb_filenames = []
    for obj in pdb_filenames_or_trajectories:
        try:  # See if MDTraj Trajectory
            tmp_filename = tempfile.mktemp(suffix=".pdb")
            obj.save_pdb(tmp_filename)
            pdb_filenames.append(tmp_filename)
        except AttributeError:  # Not an MDTraj Trajectory, assume filename
            pdb_filenames.append(obj)
    
    if PACKMOL_PATH is None:
        raise(IOError("Packmol not found, cannot run pack_box()"))
    
    output_filename = tempfile.mktemp(suffix=".pdb")

    # approximating volume to initialize box
    if size is None:
        size = approximate_volume(pdb_filenames, n_molecules_list)    

    if shape == 'box':
        box_size = size
    elif shape == 'sphere':
        sphere_radius = float(size)/2

    header = HEADER_TEMPLATE % (tolerance, output_filename, seed)
    for k in range(len(pdb_filenames)):
        filename = pdb_filenames[k]
        n_molecules = n_molecules_list[k]
        if fix and k==0 and n_molecules==1 and shape == 'box':
            header = header + FIXED_TEMPLATE % (filename, n_molecules, float(size)/2, float(size)/2, float(size)/2)
        elif fix and k==0 and n_molecules==1 and shape == 'sphere':
            header = header + FIXED_TEMPLATE % (filename, n_molecules, 0, 0, 0)
    	elif shape == 'box':
            header = header + BOX_TEMPLATE % (filename, n_molecules, box_size, box_size, box_size)
	elif shape == 'sphere':
            header = header + SPHERE_TEMPLATE % (filename, n_molecules, sphere_radius)

    pwd = os.getcwd()
    
    #print(header)
    
    packmol_filename = "packmol_input.txt"
    packmol_filename = tempfile.mktemp(suffix=".txt")
    
    file_handle = open(packmol_filename, 'w')
    file_handle.write(header)
    file_handle.close()
    
    #print(header)

    os.system("%s < %s >/dev/null" % (PACKMOL_PATH, packmol_filename)) 

    trj = md.load(output_filename)

    assert trj.topology.n_chains == sum(n_molecules_list), "Packmol error: molecules missing from output"
    
    #Begin hack to introduce bonds for the MISSING CONECT ENTRIES THAT PACKMOL FAILS TO WRITE
    
    top, bonds = trj.top.to_dataframe()

    trj_i = [md.load(filename) for filename in pdb_filenames]
    bonds_i = [t.top.to_dataframe()[1] for t in trj_i]

    offset = 0
    bonds = []
    for i in range(len(pdb_filenames)):
        n_atoms = trj_i[i].n_atoms
        for j in range(n_molecules_list[i]):        
            bonds.extend(bonds_i[i] + offset)
            offset += n_atoms

    bonds = np.array(bonds)
    trj.top = md.Topology.from_dataframe(top, bonds)
    
    if shape == 'box':
        trj.unitcell_vectors = np.array([np.eye(3)]) * box_size / 10.
    
    return trj

def approximate_volume(pdb_filenames, n_molecules_list, box_scaleup_factor=2.0):
    """Approximate the appropriate box size based on the number and types of atoms present.

    Parameters
    ----------
    pdb_filenames : list(str)
        List of pdb filenames for each component of mixture.
    n_molecules_list : list(int)
        The number of molecules of each mixture component.
    box_scaleup_factor : float, optional, default = 2.0
        Factor by which the estimated box size is increased

    Returns
    -------
    box_size : float
        The size of the box to generate.  In ANGSTROMS.

    Notes
    -----
    By default, boxes are very large for increased stability, and therefore may 
    require extra time for energy minimization and equilibration.
    """
    volume = 0.0 # in cubic angstroms
    for k, (pdb_file) in enumerate(pdb_filenames):
        molecule_volume = 0.0
        molecule_trj = md.load(pdb_filenames[k])
        for atom in molecule_trj.topology.atoms:
            if atom.element.symbol == 'H':
                molecule_volume += 5.0 # approximated from bondi radius = 1.06 angstroms
            else:
                molecule_volume += 15.0 # approximated from bondi radius of carbon = 1.53 angstroms
        volume += molecule_volume * n_molecules_list[k]
    box_size = volume**(1.0/3.0) * box_scaleup_factor
    return box_size


def approximate_volume_by_density( smiles_strings, n_molecules_list, density = 1.0, box_scaleup_factor = 1.1):
    """Generate an approximate box size based on the number and molecular weight of molecules present, and a target density for the final solvated mixture. If no density is specified, the target density is assumed to be 1 g/ml.

    Parameters
    ---------- 
    smiles_strings : list(str)
        List of smiles strings for each component of mixture.
    n_molecules_list : list(int)
        The number of molecules of each mixture component.
    box_scaleup_factor : float, optional, default = 1.1
        Factor by which the estimated box size is increased
    density : float, optional, default 1.0
        Target density for final system in g/ml

    Returns
    -------
    box_size : float
        The size (edge length) of the box to generate.  In ANGSTROMS.

    Notes
    -----
    By default, boxes are only modestly large. This approach has not been extensively tested for stability but has been used in th Mobley lab for perhaps ~100 different systems without substantial problems.
    """

    oechem = import_("openeye.oechem")

    density = density * units.grams/units.milliliter

    #Load molecules to get molecular weights
    wts = []
    mass = 0.0*units.grams/units.mole * 1./units.AVOGADRO_CONSTANT_NA #For calculating total mass
    for (idx,smi) in enumerate(smiles_strings):
        mol = oechem.OEMol()
        oechem.OEParseSmiles(mol, smi)
        wts.append( oechem.OECalculateMolecularWeight(mol)*units.grams/units.mole )
        mass += n_molecules_list[idx] * wts[idx] * 1./units.AVOGADRO_CONSTANT_NA

    #Estimate volume based on mass and density
    #Density = mass/volume so volume = mass/density (volume units are ml)
    vol = mass/density
    #Convert to box length in angstroms
    edge = vol**(1./3.)

    #Compute final box size
    box_size = edge*box_scaleup_factor/units.angstroms

    return box_size


def rename_water_atoms( pdb_filename, O_name = 'O', H1_name = 'H1', H2_name = 'H2' ):
    """Rename water atoms in a specified PDB file to have target names. Typically used to ensure a packmol-generated box containing water has water atom names corresponding to what tleap expects for standard water models.

    Parameters
    ----------
    pdb_filename : str
        The target PDB filename to edit
    O_name : str, optional, default 'O'
        Target name to set water oxygen names to
    H1_name : str, optional, default 'H1'
        Target name to set water hydrogen names to, for first hydrogen
    H2_name : str, optional, default 'H2'
        Target name to set water hydrogen names to, for second hydrogen

    Returns
    -------
    
    Notes
    -------
    Uses ParmEd to makes edits. Identifies waters by reading residues from target PDB file and identifying any residue containing three atoms with names O or O#, H or H#, and H or H# (where # is a digit or sequence of digits) as water molecules.
    """

    parmed = import_("parmed")

    pdb = parmed.load_file( pdb_filename )
    
    #Find waters and rename
    for residue in pdb.residues:
        if len(residue)==3:
            #Build list of atom types (PDB files don't store these) from names after stripping off digits
            types = []
            for atom in residue.atoms:
                name = atom.name
                while name[-1].isdigit():
                    name = name[:-1]
                types.append(name)
            #See if it's water and, if so, rename
            if 'O' in types and types.count('H')==2:
                hct = 0
                for atom in residue.atoms:
                    if 'O' in atom.name:
                        atom.name = O_name
                    elif 'H' in atom.name:
                        if hct==0:
                            atom.name = H1_name
                        else:
                            atom.name = H2_name
                        hct+=1
            
    #Write file
    pdb.write_pdb( pdb_filename )

